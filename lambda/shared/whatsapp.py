"""WhatsApp: the 'Was this you?' template message and the parse of a button tap coming back over SNS. Pure functions."""

import json
import logging
from dataclasses import dataclass

from shared.enums import PHONE_CHANNEL_ANSWERS, VerdictAnswer, WhatsAppInboundMessageType

logger = logging.getLogger(__name__)

PAYLOAD_SEPARATOR = ":"
TEMPLATE_LANGUAGE_CODE = "en_US"
EMPTY_SUMMARY_TEXT = "a request Pidge could not summarise"  # Meta rejects an empty body parameter


@dataclass(frozen=True)
class WhatsAppReply:
    request_id: str
    answer: VerdictAnswer
    sender_number: str  # E.164 with "+", built from Meta's digits-only `from`
    message_id: str  # Meta's wamid


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
        webhook_entry = json.loads(sns_message["whatsAppWebhookEntry"])
        change_value = webhook_entry["changes"][0]["value"]
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
