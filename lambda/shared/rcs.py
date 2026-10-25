"""RCS: the 'Was this you?' rich card and the parse of a chip tap coming back over SNS. Pure functions."""

import json
import logging
from dataclasses import dataclass

from shared.enums import RcsInboundMessageType, VerdictAnswer
from shared.verdict import parse_phone_answer

logger = logging.getLogger(__name__)

POSTBACK_SEPARATOR = ":"
RICH_CARD_TIME_TO_LIVE_SECONDS = 60
SUGGESTION_TEXT_MAX_CHARACTERS = 25  # RCS limit per chip


@dataclass(frozen=True)
class RcsReply:
    request_id: str
    answer: VerdictAnswer
    origination_number: str
    inbound_message_id: str


def postback_for(request_id: str, answer: VerdictAnswer) -> str:
    return f"{request_id}{POSTBACK_SEPARATOR}{answer}"


def build_rich_card(request_id: str, email_row: dict, contact_name: str, receiver_name: str, image_url: str) -> dict:
    """The card text comes from the stored email's sender address and subject, never from the model's summary."""
    sender_address = email_row.get("senderAddress") or "an unknown address"
    subject = email_row.get("subject") or "(no subject)"
    yes_text, no_text = "Yes, I sent it", "No, not me"
    assert max(len(yes_text), len(no_text)) <= SUGGESTION_TEXT_MAX_CHARACTERS
    return {
        "Content": {
            "RichCard": {
                "CardOrientation": "VERTICAL",
                "CardContent": {
                    "Title": f"{contact_name}, was this you?",
                    "Description": (
                        f"{receiver_name} got an email from {sender_address} with the subject "
                        f"\u201c{subject}\u201d. Pidge is holding it until you answer."
                    ),
                    "Media": {"FileUrl": image_url, "Height": "TALL"},
                    "Suggestions": [
                        {"Reply": {"Text": yes_text, "PostbackData": postback_for(request_id, VerdictAnswer.YES)}},
                        {"Reply": {"Text": no_text, "PostbackData": postback_for(request_id, VerdictAnswer.NO)}},
                    ],
                },
            }
        }
    }


def parse_inbound_reply(sns_message: dict) -> RcsReply | None:
    """A chip tap arrives as messageBody = JSON {type, text, postbackData}. Typed text or anything odd → None."""
    try:
        body = json.loads(sns_message["messageBody"])
        if RcsInboundMessageType(body["type"]) is not RcsInboundMessageType.SUGGESTION:
            return None
        parsed_answer = parse_phone_answer(body["postbackData"], POSTBACK_SEPARATOR)
        if parsed_answer is None:
            return None
        request_id, answer = parsed_answer
        return RcsReply(request_id, answer, sns_message["originationNumber"], sns_message["inboundMessageId"])
    except (KeyError, TypeError, ValueError):
        logger.info("inbound RCS message is not a chip tap; ignored")
        return None
