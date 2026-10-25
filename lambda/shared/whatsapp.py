"""WhatsApp: the 'Was this you?' template, the 'call Grandma?' offer and reminder, and the parse of taps and failed
statuses coming back over SNS. Pure functions."""

import json
import logging
from dataclasses import dataclass

from shared.enums import (
    PHONE_CHANNEL_ANSWERS,
    CallPromise,
    VerdictAnswer,
    WhatsAppInboundMessageType,
    WhatsAppInteractiveType,
    WhatsAppMessageStatus,
)

logger = logging.getLogger(__name__)

PAYLOAD_SEPARATOR = ":"
TEMPLATE_LANGUAGE_CODE = "en_US"
EMPTY_SUMMARY_TEXT = "a request Pidge could not summarise"  # Meta rejects an empty body parameter
CALL_BUTTON_PREFIX = "call"
CALL_BUTTON_ID_PART_COUNT = 3  # call:<requestId>:<promise>
CALL_BUTTON_TITLES = {CallPromise.TODAY: "I'll call today", CallPromise.TOMORROW: "Tomorrow"}  # Meta caps titles at 20
CALL_OFFER_BODY_TEMPLATES = {
    VerdictAnswer.NO: "Got it, the email stays held. Call {receiver_name} today?",
    VerdictAnswer.YES: "Thanks, the email goes through. Call {receiver_name} today?",
}
CALL_REMINDER_TEMPLATE = "Reminder: you said you'd call {receiver_name} today."


@dataclass(frozen=True)
class WhatsAppReply:
    request_id: str
    answer: VerdictAnswer
    sender_number: str  # E.164 with "+", built from Meta's digits-only `from`
    message_id: str  # Meta's wamid


@dataclass(frozen=True)
class CallPromiseReply:
    request_id: str  # unchecked data from the button id; the caller must match it to a request of this sender
    promise: CallPromise
    sender_number: str  # E.164 with "+", built from Meta's digits-only `from`; the only identity source
    message_id: str  # Meta's wamid


@dataclass(frozen=True)
class FailedWhatsAppStatus:
    message_id: str  # the wamid of our own message that failed
    error_code: str  # Meta's first errors[].code, as text


def payload_for(request_id: str, answer: VerdictAnswer) -> str:
    return f"{request_id}{PAYLOAD_SEPARATOR}{answer}"


def build_template_message(request_id: str, summary: str, destination_number: str, template_name: str) -> dict:
    """Meta message JSON for the approved template: body {{1}} = the sanitised summary, Yes / No quick replies."""
    quick_reply_buttons = [
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(index),
            "parameters": [{"type": "payload", "payload": payload_for(request_id, answer)}],
        }
        for index, answer in enumerate((VerdictAnswer.YES, VerdictAnswer.NO))
    ]
    return {
        "messaging_product": "whatsapp",
        "to": destination_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": TEMPLATE_LANGUAGE_CODE},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": summary.strip() or EMPTY_SUMMARY_TEXT}]},
                *quick_reply_buttons,
            ],
        },
    }


def parse_inbound_reply(sns_message: dict) -> WhatsAppReply | None:
    """A tap arrives inside the JSON string `whatsAppWebhookEntry` as a message of type `button`.

    Delivery statuses, typed text and anything odd → None. Only the first message of the first change is read.
    """
    try:
        change_value = read_first_change_value(sns_message)
        if "messages" not in change_value:
            return None  # a sent / delivered / read status for one of our own messages
        message = change_value["messages"][0]
        if WhatsAppInboundMessageType(message["type"]) is not WhatsAppInboundMessageType.BUTTON:
            return None
        request_id, answer_value = message["button"]["payload"].rsplit(PAYLOAD_SEPARATOR, 1)
        answer = VerdictAnswer(answer_value)
        if answer not in PHONE_CHANNEL_ANSWERS:
            return None  # undo and reverse exist only in the PWA
        return WhatsAppReply(request_id, answer, f"+{message['from']}", message["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        logger.info("inbound WhatsApp message is not a button tap; ignored")
        return None


def call_button_id(request_id: str, promise: CallPromise) -> str:
    return PAYLOAD_SEPARATOR.join((CALL_BUTTON_PREFIX, request_id, str(promise)))


def build_call_offer_message(request_id: str, receiver_name: str, destination_number: str, answer: VerdictAnswer) -> dict:
    """Meta JSON for a free-form reply-button message (sent inside the 24-h window the contact's tap opened).

    The body is fixed text plus the receiver's name; no email text ever goes in. Raises ValueError unless answer is YES or NO.
    """
    if answer not in CALL_OFFER_BODY_TEMPLATES:
        raise ValueError(f"no call offer for answer {answer!r}")
    reply_buttons = [
        {"type": "reply", "reply": {"id": call_button_id(request_id, promise), "title": CALL_BUTTON_TITLES[promise]}}
        for promise in (CallPromise.TODAY, CallPromise.TOMORROW)
    ]
    return {
        "messaging_product": "whatsapp",
        "to": destination_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": CALL_OFFER_BODY_TEMPLATES[answer].format(receiver_name=receiver_name)},
            "action": {"buttons": reply_buttons},
        },
    }


def build_call_reminder_message(receiver_name: str, destination_number: str) -> dict:
    """Meta JSON for the plain-text reminder; fixed text plus the receiver's name."""
    return {
        "messaging_product": "whatsapp",
        "to": destination_number,
        "type": "text",
        "text": {"body": CALL_REMINDER_TEMPLATE.format(receiver_name=receiver_name)},
    }


def read_first_change_value(sns_message: dict) -> dict:
    """The `value` of the first change inside the JSON string `whatsAppWebhookEntry`; raises on any other shape."""
    webhook_entry = json.loads(sns_message["whatsAppWebhookEntry"])
    return webhook_entry["changes"][0]["value"]


def parse_call_promise_reply(sns_message: dict) -> CallPromiseReply | None:
    """A tap on the call offer: a message of type `interactive` / `button_reply` with id `call:<requestId>:<promise>`.

    Template taps (`button`), statuses, typed text and any other id → None. Only the first message is read.
    """
    try:
        change_value = read_first_change_value(sns_message)
        if "messages" not in change_value:
            return None
        message = change_value["messages"][0]
        if WhatsAppInboundMessageType(message["type"]) is not WhatsAppInboundMessageType.INTERACTIVE:
            return None
        interactive = message["interactive"]
        if WhatsAppInteractiveType(interactive["type"]) is not WhatsAppInteractiveType.BUTTON_REPLY:
            return None
        id_parts = interactive["button_reply"]["id"].split(PAYLOAD_SEPARATOR)
        if len(id_parts) != CALL_BUTTON_ID_PART_COUNT or id_parts[0] != CALL_BUTTON_PREFIX or not id_parts[1]:
            return None
        return CallPromiseReply(id_parts[1], CallPromise(id_parts[2]), f"+{message['from']}", message["id"])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        logger.info("inbound WhatsApp message is not a call promise; ignored")
        return None


def parse_failed_status(sns_message: dict) -> FailedWhatsAppStatus | None:
    """A `failed` delivery status for one of our own messages, with Meta's first error code. Anything else → None."""
    try:
        change_value = read_first_change_value(sns_message)
        if "statuses" not in change_value:
            return None
        status = change_value["statuses"][0]
        if WhatsAppMessageStatus(status["status"]) is not WhatsAppMessageStatus.FAILED:
            return None
        return FailedWhatsAppStatus(status["id"], str(status["errors"][0]["code"]))
    except (KeyError, IndexError, TypeError, ValueError):
        logger.info("WhatsApp status is not a failure; ignored")
        return None
