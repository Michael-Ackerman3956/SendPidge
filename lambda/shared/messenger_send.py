"""A resident's message to someone in her circle (pidge-send). The recipient must be an address Pidge may ask of one of her
own contacts; the sender is her own RECEIVER# address (email) or Pidge's number, never anything from the body. Refusals run in
order (channel allowed, recipient, held thread, WhatsApp window, the hourly send cap) and write nothing; past them the
MESSAGE#<channel>#out-<uuid4> row is written sending, the provider is called, and the row becomes sent or failed. Logs carry
ids and outcomes only: never the text, an address or a number."""

import hmac
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from botocore.exceptions import ClientError

from shared import channels, graph, hourly_counters, inbox_rows, verdict, whatsapp
from shared.enums import ActionOutcome, Channel, RequestState
from shared.messenger_enums import MessageDirection, ProviderStatus
from shared.profiles import ReceiverProfile, normalize_receiver_address
from shared.reach_points import reach_channel_of

logger = logging.getLogger(__name__)

SENDS_PER_HOUR = 30
WHATSAPP_WINDOW = timedelta(hours=24)  # Meta takes free-form text only this long after the person's last message
TEXT_MAX_CHARACTERS = 1_000
SEND_COUNTER_PARTITION_PREFIX = "SENDRATE#"  # SENDRATE#<userId>/HOUR#<yyyy-mm-ddThh>, through hourly_counters.take_hourly_slot
OUT_MESSAGE_ID_PREFIX = "out-"  # an outgoing row's id is <channel>#out-<uuid4>: the provider's id does not exist before the send
EMAIL_SUBJECT_TEMPLATE = "Message from {resident_name}"
TEXT_MESSAGE_TYPE = "TRANSACTIONAL"
THREAD_CHANNELS = (Channel.EMAIL, Channel.WHATSAPP, Channel.RCS, Channel.SMS)  # every channel a send names and a hold comes in on
PHONE_CHANNEL_FLAGS = ((Channel.WHATSAPP, "WHATSAPP_ENABLED"), (Channel.RCS, "RCS_ENABLED"), (Channel.SMS, "SMS_ENABLED"))
HOLDING_STATES = frozenset({RequestState.PENDING, RequestState.CONFIRMED_PENDING})


@dataclass(frozen=True)
class SendRequest:
    channel: Channel
    to: str  # as sent; compared with her contacts' askable addresses (an email case-insensitively)
    text: str  # stripped, 1 to TEXT_MAX_CHARACTERS characters


@dataclass(frozen=True)
class SendResult:
    outcome: ActionOutcome  # RECORDED, or what is sent back as action_rejected
    message_row: dict | None  # the stored outgoing row once the provider answered (sent or failed); None for a refusal


def parse_send_request(body: dict) -> SendRequest:
    """channel, to and text; any other field ("from" included) is never read. ValueError when one is missing or not text,
    the channel is not one a send names, or the stripped text is empty or longer than TEXT_MAX_CHARACTERS."""
    try:
        channel, to, text = Channel(body["channel"]), body["to"], body["text"]
    except (KeyError, TypeError) as error:
        raise ValueError("send_message needs channel, to and text") from error
    if channel not in THREAD_CHANNELS:
        raise ValueError(f"nothing is sent on {channel}")
    if not isinstance(to, str) or not to:
        raise ValueError("to is not text")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= TEXT_MAX_CHARACTERS:
        raise ValueError(f"text must be 1 to {TEXT_MAX_CHARACTERS} characters")
    return SendRequest(channel, to, text.strip())


def is_messenger_resident(resident: ReceiverProfile) -> bool:
    """Pidge's WhatsApp, RCS and SMS numbers are one resident's inbox (MESSENGER_RESIDENT_ADDRESS). Unset or "" (a stack
    staging the messenger) means no resident is that one."""
    messenger_address = os.environ.get("MESSENGER_RESIDENT_ADDRESS")
    return messenger_address is not None and normalize_receiver_address(messenger_address) == resident.receiver_address


def send_channels_for(resident: ReceiverProfile) -> tuple[Channel, ...]:
    """Email for every resident (from her own address). WhatsApp, RCS and SMS only for the messenger resident, since a reply
    to Pidge's number lands in her inbox, and each only while its stack flag is "true"."""
    if not is_messenger_resident(resident):
        return (Channel.EMAIL,)
    return (Channel.EMAIL, *(channel for channel, flag_name in PHONE_CHANNEL_FLAGS if os.environ.get(flag_name) == "true"))


def is_same_address(channel: Channel, stored_address: str, to: str) -> bool:
    """Constant time; an email compares case-insensitively, a number exactly."""
    if channel is Channel.EMAIL:
        return hmac.compare_digest(stored_address.casefold().encode(), to.casefold().encode())
    return hmac.compare_digest(stored_address.encode(), to.encode())


def recipient_for(resident: ReceiverProfile, channel: Channel, to: str) -> graph.Contact | None:
    """The contact on her own non-PENDING CONTACT# rows with `to` among the addresses Pidge may ask on the channel's reach
    channel (an SMS goes to a checked RCS number); None when nobody has it."""
    reach_channel = reach_channel_of(channel)
    for contact in graph.list_contacts(resident.receiver_address):
        if any(is_same_address(channel, address, to) for address in contact.addresses_to_ask(reach_channel)):
            return contact
    return None


def is_own_domain_address(resident: ReceiverProfile, request: SendRequest) -> bool:
    """An email to Pidge's receiving domain (every resident's address is on it) or its sending domain (RECEIPT_SENDER's)
    would come back in through our own mail flow."""
    if request.channel is not Channel.EMAIL:
        return False
    own_domains = (resident.receiver_address.rpartition("@")[2], os.environ["RECEIPT_SENDER"].rpartition("@")[2])
    return request.to.casefold().rpartition("@")[2] in {domain.casefold() for domain in own_domains}


def is_request_holding(request_id: str, now_text: str) -> bool:
    """A REQUEST# row past its 30-day retention is deleted by TTL, long after it stopped holding: it holds nothing."""
    request_row = graph.get_request(request_id)
    if request_row is None:
        logger.info("held request %s is past its retention; it no longer holds its thread", request_id)
        return False
    return verdict.effective_status(request_row, now_text) in HOLDING_STATES


def is_thread_held(resident: ReceiverProfile, channel: Channel, to: str, now: datetime) -> bool:
    """True while a hold recorded on this address's thread, on any channel sharing the send channel's reach channel (an SMS
    hold also holds an RCS send to that number), is pending or confirmed_pending."""
    now_text = graph.iso_timestamp(now)
    for thread_channel in THREAD_CHANNELS:
        if reach_channel_of(thread_channel) is not reach_channel_of(channel):
            continue
        thread_row = inbox_rows.get_thread(resident.receiver_address, inbox_rows.thread_key_for(thread_channel, to))
        if thread_row is None or "heldRequestIds" not in thread_row:
            continue
        if any(is_request_holding(request_id, now_text) for request_id in thread_row["heldRequestIds"]):
            return True
    return False


def is_whatsapp_window_open(resident: ReceiverProfile, to: str, now: datetime) -> bool:
    """Open within WHATSAPP_WINDOW of the number's last message to Pidge. No THREAD row, or no lastInboundAt on it, means
    nothing came in: closed."""
    thread_row = inbox_rows.get_thread(resident.receiver_address, inbox_rows.thread_key_for(Channel.WHATSAPP, to))
    if thread_row is None or "lastInboundAt" not in thread_row:
        return False
    return now - verdict.parse_timestamp(thread_row["lastInboundAt"]) < WHATSAPP_WINDOW


def send_message(resident: ReceiverProfile, request: SendRequest, now: datetime) -> SendResult:
    """The refusals in order, each writing nothing and costing no slot; then the hourly slot (a failed counter write is
    OVER_CAP), the outgoing row written sending, and the provider call."""
    if request.channel not in send_channels_for(resident) or is_own_domain_address(resident, request):
        return refuse(resident, ActionOutcome.NOT_ALLOWED)
    recipient = recipient_for(resident, request.channel, request.to)
    if recipient is None:
        return refuse(resident, ActionOutcome.NOT_A_CONTACT)
    if is_thread_held(resident, request.channel, request.to, now):
        return refuse(resident, ActionOutcome.THREAD_HELD)
    if request.channel is Channel.WHATSAPP and not is_whatsapp_window_open(resident, request.to, now):
        return refuse(resident, ActionOutcome.CHANNEL_CLOSED)
    if not hourly_counters.take_hourly_slot(f"{SEND_COUNTER_PARTITION_PREFIX}{resident.receiver_user_id}", SENDS_PER_HOUR):
        return refuse(resident, ActionOutcome.OVER_CAP)
    item_id = inbox_rows.message_item_id(request.channel, f"{OUT_MESSAGE_ID_PREFIX}{uuid.uuid4()}")
    inbox_rows.put_out_message(build_out_message_row(resident, request, recipient, item_id, now))
    return deliver(resident, request, item_id)


def refuse(resident: ReceiverProfile, outcome: ActionOutcome) -> SendResult:
    logger.info("send by %s refused: %s", resident.receiver_user_id, outcome)
    return SendResult(outcome, None)


def build_out_message_row(
    resident: ReceiverProfile, request: SendRequest, recipient: graph.Contact, item_id: str, now: datetime,
) -> dict:
    sent_at = graph.iso_timestamp(now)
    return {
        **inbox_rows.out_message_key(item_id),
        "channel": str(request.channel),
        "direction": str(MessageDirection.OUT),
        "threadKey": inbox_rows.thread_key_for(request.channel, request.to),
        "recipientAddress": request.to,
        "recipientName": recipient.contact_name,
        "textBody": request.text,
        "sentAt": sent_at,
        "receiverEmail": resident.receiver_address,
        "providerStatus": str(ProviderStatus.SENDING),
        graph.USER_INDEX_PARTITION_KEY: graph.user_index_partition(resident.receiver_user_id, inbox_rows.MESSAGE_ROW_KIND),
        graph.USER_INDEX_SORT_KEY: sent_at,
        "ttlEpochSeconds": graph.retention_epoch(graph.EMAIL_RETENTION_SECONDS),
    }


def deliver(resident: ReceiverProfile, request: SendRequest, item_id: str) -> SendResult:
    """The channel's provider call. Any failure keeps the row as failed and answers SEND_FAILED; it is logged by id and
    error code only (a provider's message may quote the number)."""
    try:
        provider_message_id = PROVIDER_SENDS[request.channel](resident, request)
    except Exception as error:
        logger.error("send %s failed: %s", item_id, error_code_of(error))
        return SendResult(ActionOutcome.SEND_FAILED, inbox_rows.mark_out_message(item_id, ProviderStatus.FAILED, None))
    logger.info("send %s sent as %s", item_id, provider_message_id)
    return SendResult(ActionOutcome.RECORDED, inbox_rows.mark_out_message(item_id, ProviderStatus.SENT, provider_message_id))


def error_code_of(error: Exception) -> str:
    return error.response["Error"]["Code"] if isinstance(error, ClientError) else type(error).__name__


def send_by_email(resident: ReceiverProfile, request: SendRequest) -> str:
    """SES from her own RECEIVER# address, so a reply comes back to her inbox through Mail Manager."""
    response = channels.get_ses_client().send_email(
        FromEmailAddress=resident.receiver_address,
        Destination={"ToAddresses": [request.to]},
        Content={"Simple": {
            "Subject": {"Data": EMAIL_SUBJECT_TEMPLATE.format(resident_name=resident.resident_name)},
            "Body": {"Text": {"Data": request.text}},
        }},
    )
    return response["MessageId"]


def send_by_whatsapp(resident: ReceiverProfile, request: SendRequest) -> str:
    """Free-form text from Pidge's WhatsApp number, inside the window is_whatsapp_window_open checked."""
    return channels.send_whatsapp_message(whatsapp.build_text_message(request.to, request.text))


def send_text_message(request: SendRequest, origination_identity: str) -> str:
    response = channels.get_rcs_client().send_text_message(
        DestinationPhoneNumber=request.to, OriginationIdentity=origination_identity, MessageBody=request.text,
        MessageType=TEXT_MESSAGE_TYPE,
    )
    return response["MessageId"]


def send_by_sms(resident: ReceiverProfile, request: SendRequest) -> str:
    return send_text_message(request, os.environ["SMS_PHONE_NUMBER_ARN"])


def send_by_rcs(resident: ReceiverProfile, request: SendRequest) -> str:
    """Plain text with the RCS agent's ARN as the origination identity (not yet run against a live agent)."""
    return send_text_message(request, os.environ["RCS_AGENT_ARN"])


PROVIDER_SENDS = {Channel.EMAIL: send_by_email, Channel.WHATSAPP: send_by_whatsapp, Channel.SMS: send_by_sms, Channel.RCS: send_by_rcs}
