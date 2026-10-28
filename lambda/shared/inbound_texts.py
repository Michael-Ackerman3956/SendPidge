"""Typed texts and media sent to Pidge's WhatsApp, RCS and SMS numbers, parsed from their SNS records into one shape. Taps,
statuses and message types that make no inbox row are classified and give nothing (logged by id); a broken envelope raises.
Pure functions."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from shared import graph
from shared.channel_enums import WHATSAPP_MEDIA_MESSAGE_TYPES, RcsInboundMessageType, WhatsAppInboundMessageType
from shared.enums import Channel

logger = logging.getLogger(__name__)

WHATSAPP_NUMBER_DIGITS_PATTERN = re.compile(r"[0-9]{1,15}")  # Meta's `from`, used with fullmatch: E.164 without the "+"


@dataclass(frozen=True)
class ReceivedText:
    channel: Channel
    provider_message_id: str  # Meta's wamid, or the RCS / SMS inboundMessageId
    sender_address: str  # E.164 with "+"; on WhatsApp without `from`, the business-scoped user id as sent
    sender_profile_name: str  # the WhatsApp profile name, "" elsewhere: sender-controlled, never stored as senderName
    text: str | None  # None: media (never downloaded, its caption ignored)
    sent_at: str | None  # WhatsApp's send time as a stored timestamp; None on RCS and SMS


def require_text_field(container: dict, field_name: str) -> str:
    """The field as non-empty text; missing raises KeyError, anything else ValueError."""
    value = container[field_name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} is not text")
    return value


def require_body_text(container: dict, field_name: str) -> str:
    """A message body: text, possibly empty; anything else raises ValueError."""
    value = container[field_name]
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is not text")
    return value


def known_member_of(enum_type: type[StrEnum], raw_value) -> StrEnum | None:
    """The member whose wire value this is; None for a value the enum does not know."""
    return next((member for member in enum_type if member.value == raw_value), None)


def parse_sms_record(sns_message: dict, sms_origination_number: str) -> list[ReceivedText]:
    """One inbound SMS. A record addressed to any number but ours raises ValueError: it is never read as ours."""
    if require_text_field(sns_message, "destinationNumber") != sms_origination_number:
        raise ValueError("SMS record for a number that is not Pidge's")
    return [ReceivedText(Channel.SMS, require_text_field(sns_message, "inboundMessageId"),
                         require_text_field(sns_message, "originationNumber"), "", require_body_text(sns_message, "messageBody"),
                         None)]


def parse_rcs_record(sns_message: dict) -> list[ReceivedText]:
    """Typed RCS text (messageBody JSON {type TEXT, text}). A chip tap is pidge-verdict's and any other type makes no row:
    both give nothing."""
    message_id = require_text_field(sns_message, "inboundMessageId")
    body = json.loads(require_text_field(sns_message, "messageBody"))
    if known_member_of(RcsInboundMessageType, body["type"]) is not RcsInboundMessageType.TEXT:
        logger.info("RCS message %s is a chip tap or a type with no inbox row; skipped", message_id)
        return []
    return [ReceivedText(Channel.RCS, message_id, require_text_field(sns_message, "originationNumber"), "",
                         require_body_text(body, "text"), None)]


def parse_whatsapp_record(sns_message: dict) -> list[ReceivedText]:
    """Every message of every change in the JSON string `whatsAppWebhookEntry`, as one entry {id, changes} or as Meta's
    {object, entry[]} wrapper. Typed text and media give a ReceivedText; taps, statuses and other types give nothing."""
    webhook_entry = json.loads(sns_message["whatsAppWebhookEntry"])
    entries = webhook_entry["entry"] if "entry" in webhook_entry else [webhook_entry]
    return [received for entry in entries for change in entry["changes"] for received in received_texts_of_change(change["value"])]


def received_texts_of_change(change_value: dict) -> list[ReceivedText]:
    if "messages" not in change_value:
        return []  # a status for one of our own messages, or another webhook field: nothing was sent to Pidge
    received_texts = []
    for message in change_value["messages"]:
        received_text = received_text_of_message(message, change_value)
        if received_text is not None:
            received_texts.append(received_text)
    return received_texts


def received_text_of_message(message: dict, change_value: dict) -> ReceivedText | None:
    """Typed text, or media with text None. A tap (`button`, `interactive`) is pidge-verdict's and a type with no inbox row
    (reaction, location, unknown) is skipped; both None, logged by id."""
    message_id = require_text_field(message, "id")
    message_type = known_member_of(WhatsAppInboundMessageType, message["type"])
    if message_type is WhatsAppInboundMessageType.TEXT:
        text = require_body_text(message["text"], "body")
    elif message_type in WHATSAPP_MEDIA_MESSAGE_TYPES:
        text = None
    else:
        logger.info("WhatsApp message %s is a tap or a type with no inbox row; skipped", message_id)
        return None
    sender_address, sender_digits = whatsapp_sender_of(message)
    sent_at = graph.iso_timestamp(datetime.fromtimestamp(int(message["timestamp"]), timezone.utc))
    return ReceivedText(Channel.WHATSAPP, message_id, sender_address, profile_name_of(change_value, sender_digits), text, sent_at)


def whatsapp_sender_of(message: dict) -> tuple[str, str | None]:
    """(sender address, Meta's `from` digits): "+" and `from`, else `from_user_id` as sent with digits None (since April 2026
    a sender may carry only that business-scoped id). Neither, or a `from` that is not a number, raises ValueError."""
    if "from" in message:
        sender_digits = require_text_field(message, "from")
        if not WHATSAPP_NUMBER_DIGITS_PATTERN.fullmatch(sender_digits):
            raise ValueError("WhatsApp `from` is not a number")
        return f"+{sender_digits}", sender_digits
    if "from_user_id" in message:
        return require_text_field(message, "from_user_id"), None
    raise ValueError("WhatsApp message has neither from nor from_user_id")


def profile_name_of(change_value: dict, sender_digits: str | None) -> str:
    """The profile.name of the contacts[] entry whose wa_id is the sender's `from`; "" when there is no such entry."""
    if sender_digits is None or "contacts" not in change_value:
        return ""
    for contact in change_value["contacts"]:
        if "wa_id" in contact and contact["wa_id"] == sender_digits and "profile" in contact and "name" in contact["profile"]:
            return require_body_text(contact["profile"], "name")
    return ""
