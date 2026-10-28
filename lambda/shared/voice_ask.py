"""The call on the re-ask and its hang-up. Pidge calls the contact's first checked RCS number once (pidge-retry, attempt 1)
through Amazon Connect, carrying the keypad prompt and the AI agent's opening line, each only fixed words, the resident's name
and a channel phrase; the hourly cap and a hang-up booked before the call bound the cost. Every failure is logged with ids
only and counted, not raised."""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared import graph, hourly_counters, inbox_rows, metrics, reach_points, schedules, verdict
from shared.enums import Channel

logger = logging.getLogger(__name__)

VOICE_ASK_FAILED_METRIC_NAME = "VoiceAskFailed"
CALL_COUNTER_PARTITION_KEY = "CALLRATE#stack"
INSTANCE_ARN_PATTERN = re.compile(r"arn:aws:connect:(?P<region>[a-z0-9-]+):\d{12}:instance/(?P<instance_id>[0-9a-f-]{36})")
SPOKEN_TEXT_PATTERN = re.compile(r"[A-Za-z0-9 ,.'?-]{1,400}")  # plain words only: no SSML, no line breaks, no other script
CHANNEL_PHRASES = {
    Channel.EMAIL: "an email", Channel.WHATSAPP: "a WhatsApp message", Channel.SMS: "a text message", Channel.RCS: "a text message",
}
CALL_ALREADY_ENDED_ERROR_CODES = frozenset({"ContactNotFoundException"})

_connect_clients: dict[str, object] = {}
_qconnect_clients: dict[str, object] = {}


def get_connect_client(region: str):
    """One Connect client per Region (the instance's, parsed from its ARN), kept for the container's life."""
    if region not in _connect_clients:
        _connect_clients[region] = boto3.client("connect", region_name=region)
    return _connect_clients[region]


def get_qconnect_client(region: str):
    """One Amazon Q in Connect client per Region (the instance's), kept for the container's life."""
    if region not in _qconnect_clients:
        _qconnect_clients[region] = boto3.client("qconnect", region_name=region)
    return _qconnect_clients[region]


def build_call_greeting(resident_name: str, inbound_channel: Channel) -> str:
    """The two sentences every call opens with; ValueError for a channel with no phrase."""
    if inbound_channel not in CHANNEL_PHRASES:
        raise ValueError(f"no spoken phrase for {inbound_channel}")
    return (f"Hello, this is Pidge, calling for {resident_name}. "
            f"{resident_name} just got {CHANNEL_PHRASES[inbound_channel]} in your name.")


def require_spoken_text(text: str) -> str:
    """The text itself when it fullmatches SPOKEN_TEXT_PATTERN; ValueError otherwise."""
    if SPOKEN_TEXT_PATTERN.fullmatch(text) is None:
        raise ValueError("the text holds words Pidge may not speak")
    return text


def build_ask_prompt(resident_name: str, inbound_channel: Channel) -> str:
    """The keypad sentence the call speaks. Where a voice Yes would be refused (an SMS or RCS hold) it offers only the No.
    Raises ValueError for a channel with no phrase or a sentence outside SPOKEN_TEXT_PATTERN."""
    greeting = build_call_greeting(resident_name, inbound_channel)
    if verdict.is_yes_refused_on(Channel.VOICE, inbound_channel):
        return require_spoken_text(f"{greeting} If you did not send it, press 2. If you did send it, say so in the Pidge app.")
    return require_spoken_text(f"{greeting} If you sent it, press 1. If you did not send it, press 2.")


def build_ai_opening(resident_name: str, inbound_channel: Channel) -> str:
    """The AI agent's first line on the call. Raises ValueError for a channel with no phrase or a sentence outside
    SPOKEN_TEXT_PATTERN."""
    return require_spoken_text(f"{build_call_greeting(resident_name, inbound_channel)} Did you send it?")


def parse_instance_arn(instance_arn: str) -> tuple[str, str]:
    """(region, instance_id) of a Connect instance ARN; ValueError when it is not one."""
    match = INSTANCE_ARN_PATTERN.fullmatch(instance_arn)
    if match is None:
        raise ValueError("not a Connect instance ARN")
    return match["region"], match["instance_id"]


def place_call(
    request_id: str, destination_number: str, prompt: str, ai_opening: str, attempt: int, retry_function_arn: str
) -> str:
    """Books the hang-up, then starts the call and stores its ContactId, which it returns. Raises on any failure, so a call
    never starts without its hang-up; a call whose ContactId could not be stored is stopped first (no answer on it could
    count, and the hang-up would not find it)."""
    region, instance_id = parse_instance_arn(os.environ["VOICE_INSTANCE_ARN"])
    hang_up_at = datetime.now(timezone.utc) + timedelta(seconds=int(os.environ["VOICE_CALL_MAX_SECONDS"]))
    schedules.schedule_one_time_invocation(
        f"hangup-{request_id}-{attempt}", hang_up_at, retry_function_arn, {"hangUpRequestId": request_id})
    response = get_connect_client(region).start_outbound_voice_contact(
        InstanceId=instance_id, ContactFlowId=os.environ["VOICE_CONTACT_FLOW_ID"], DestinationPhoneNumber=destination_number,
        SourcePhoneNumber=os.environ["VOICE_SOURCE_PHONE_NUMBER"],
        Attributes={"requestId": request_id, "askPrompt": prompt, "aiOpening": ai_opening},
        ClientToken=f"{request_id}-call-{attempt}",  # deterministic: a retried start returns the same contact
    )
    contact_id = response["ContactId"]
    try:
        graph.record_voice_contact_id(request_id, contact_id)
    except (BotoCoreError, ClientError):
        stop_unbound_call(region, instance_id, request_id, contact_id)
        raise
    return contact_id


def stop_unbound_call(region: str, instance_id: str, request_id: str, contact_id: str) -> None:
    """Best effort; a failed stop is logged (the caller counts the original failure) and the flow's own timeouts end the call."""
    try:
        get_connect_client(region).stop_contact(InstanceId=instance_id, ContactId=contact_id)
    except (BotoCoreError, ClientError) as error:
        logger.error("could not stop call %s of %s after its id was not stored: %s", contact_id, request_id, type(error).__name__)


def report_voice_failure(request_id: str, error: Exception) -> None:
    """Error log with the request id and the exception class only (an AWS message may carry the number), and VoiceAskFailed."""
    logger.error("voice ask for %s failed: %s", request_id, type(error).__name__)
    metrics.emit_count_metric(VOICE_ASK_FAILED_METRIC_NAME)


def call_contact_on_re_ask(
    request_row: dict, contact: graph.Contact, resident_name: str, attempt: int, retry_function_arn: str
) -> str | None:
    """Calls the contact's first checked RCS number once; returns the ContactId, or None when voice is off, there is no
    checked number, the hourly slot is refused, or anything failed (logged and counted). Never raises once VOICE_ENABLED is
    read; the stack always sets it, so an unset one is a broken deploy and raises KeyError."""
    if os.environ["VOICE_ENABLED"] != "true":
        return None
    request_id = graph.request_id_of(request_row)
    destinations = contact.addresses_to_ask(reach_points.reach_channel_of(Channel.VOICE))
    if not destinations:
        logger.info("no call for %s: the contact has no checked number", request_id)
        return None
    try:
        inbound_channel = inbox_rows.inbound_channel_of(request_row)
        prompt = build_ask_prompt(resident_name, inbound_channel)
        ai_opening = build_ai_opening(resident_name, inbound_channel)
        if not hourly_counters.take_hourly_slot(CALL_COUNTER_PARTITION_KEY, int(os.environ["VOICE_CALLS_PER_HOUR"])):
            logger.warning("no call for %s: the hourly call slot was refused (cap reached or counter write failed)", request_id)
            return None
        contact_id = place_call(request_id, destinations[0], prompt, ai_opening, attempt, retry_function_arn)
    except (BotoCoreError, ClientError, ValueError, KeyError) as error:
        report_voice_failure(request_id, error)
        return None
    logger.info("called the contact of %s as %s", request_id, contact_id)
    return contact_id


def hang_up_call(request_id: str) -> None:
    """The hang-up timer fired: stops the call stored on the request. No call stored, or one that already ended: info log.
    A failed stop is logged and counted, never raised; a failed read of the request raises (Lambda retries the invocation)."""
    request_row = graph.get_request(request_id)
    if request_row is None or "voiceContactId" not in request_row:
        logger.info("hang-up for %s: no call was placed", request_id)
        return
    contact_id = request_row["voiceContactId"]
    try:
        region, instance_id = parse_instance_arn(os.environ["VOICE_INSTANCE_ARN"])
        get_connect_client(region).stop_contact(InstanceId=instance_id, ContactId=contact_id)
    except ClientError as error:
        if error.response["Error"]["Code"] in CALL_ALREADY_ENDED_ERROR_CODES:
            logger.info("hang-up for %s: call %s already ended", request_id, contact_id)
            return
        report_voice_failure(request_id, error)
        return
    except (BotoCoreError, ValueError, KeyError) as error:
        report_voice_failure(request_id, error)
        return
    logger.info("hung up call %s of %s", contact_id, request_id)
