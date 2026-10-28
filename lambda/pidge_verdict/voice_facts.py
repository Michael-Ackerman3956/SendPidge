"""The four facts the AI agent may speak, written to its Amazon Q in Connect session at the load_facts step, before the bot.

Every value is code-derived: the resident's enrolled name, the inbound channel and the Reader's intent as fixed phrases, and
the whole minutes since the hold was created. Nothing the sender wrote (summary, claimed person, subject, body, sender name
or address, amounts, links) and no contact name or number is written.
"""

import logging
import os
import re
from datetime import datetime

from shared import graph, inbox_rows, verdict, voice_ask
from shared.enums import ActionOutcome, Channel, Intent

logger = logging.getLogger(__name__)

VOICE_SESSION_FACT_KEYS = ("residentName", "messageKind", "askedFor", "minutesAgo")
INTENT_PHRASES = {
    Intent.MONEY: "asks for money", Intent.CODE: "asks for a code", Intent.URGENT_ACTION: "asks for urgent action",
    Intent.NONE: "asks for something",
}
SESSION_DATA_NAMESPACE = "Custom"  # the prompt reads these as {{$.Custom.<key>}}
ASSISTANT_ARN_PATTERN = re.compile(r"arn:aws:wisdom:(?P<region>[a-z0-9-]+):\d{12}:assistant/(?P<assistant_id>[0-9a-f-]{36})")
SESSION_ARN_PATTERN = re.compile(
    r"arn:aws:wisdom:[a-z0-9-]+:\d{12}:session/(?P<assistant_id>[0-9a-f-]{36})/(?P<session_id>[0-9a-f-]{36})")


def build_session_facts(request_row: dict, resident_name: str, now: datetime) -> dict[str, str]:
    """The four facts, keyed in VOICE_SESSION_FACT_KEYS order. ValueError for a channel or intent with no phrase, or a value
    outside SPOKEN_TEXT_PATTERN."""
    inbound_channel = inbox_rows.inbound_channel_of(request_row)
    if inbound_channel not in voice_ask.CHANNEL_PHRASES:
        raise ValueError(f"no spoken phrase for {inbound_channel}")
    seconds_since_created = (now - verdict.parse_timestamp(request_row["createdAt"])).total_seconds()
    fact_values = (
        resident_name,
        voice_ask.CHANNEL_PHRASES[inbound_channel],
        INTENT_PHRASES[Intent(request_row["verdict"]["intent"])],
        str(max(0, int(seconds_since_created) // 60)),
    )
    return {key: voice_ask.require_spoken_text(value) for key, value in zip(VOICE_SESSION_FACT_KEYS, fact_values, strict=True)}


def load_session_facts(request_row: dict, resident_name: str, contact_id: str, now: datetime) -> ActionOutcome:
    """Writes the facts to this contact's session; RECORDED only after UpdateSessionData succeeded. No assistant configured:
    NOT_ALLOWED (the flow must not reach load_facts before the agent exists). A hold where a voice Yes is refused (SMS, RCS):
    WRONG_CHANNEL, nothing written, and the flow's keypad offers only the No. Any other failure raises, and Connect's Error
    branch goes to the keypad."""
    assistant_arn = os.environ["VOICE_AI_ASSISTANT_ARN"]
    if not assistant_arn:
        return ActionOutcome.NOT_ALLOWED
    if verdict.is_yes_refused_on(Channel.VOICE, inbox_rows.inbound_channel_of(request_row)):
        logger.info("no AI session for %s: a voice Yes is refused on this hold, so the keypad asks", graph.request_id_of(request_row))
        return ActionOutcome.WRONG_CHANNEL
    facts = build_session_facts(request_row, resident_name, now)
    region, instance_id = voice_ask.parse_instance_arn(os.environ["VOICE_INSTANCE_ARN"])
    assistant_id = parse_assistant_id(assistant_arn, region)
    session_id = find_contact_session_id(region, instance_id, contact_id, assistant_id)
    voice_ask.get_qconnect_client(region).update_session_data(
        assistantId=assistant_id, sessionId=session_id, namespace=SESSION_DATA_NAMESPACE,
        data=[{"key": key, "value": {"stringValue": value}} for key, value in facts.items()],
    )
    logger.info("wrote the AI session facts for %s on contact %s", graph.request_id_of(request_row), contact_id)
    return ActionOutcome.RECORDED


def parse_assistant_id(assistant_arn: str, region: str) -> str:
    """The assistant id of a Q in Connect assistant ARN in the instance's Region; ValueError otherwise."""
    match = ASSISTANT_ARN_PATTERN.fullmatch(assistant_arn)
    if match is None or match["region"] != region:
        raise ValueError("VOICE_AI_ASSISTANT_ARN is not an assistant in the instance's Region")
    return match["assistant_id"]


def find_contact_session_id(region: str, instance_id: str, contact_id: str, assistant_id: str) -> str:
    """The session id Connect opened for this contact (DescribeContact WisdomInfo.SessionArn); ValueError when the ARN is
    not a session of our assistant."""
    contact = voice_ask.get_connect_client(region).describe_contact(InstanceId=instance_id, ContactId=contact_id)["Contact"]
    match = SESSION_ARN_PATTERN.fullmatch(contact["WisdomInfo"]["SessionArn"])
    if match is None or match["assistant_id"] != assistant_id:
        raise ValueError("the contact's session is not one of our assistant")
    return match["session_id"]
