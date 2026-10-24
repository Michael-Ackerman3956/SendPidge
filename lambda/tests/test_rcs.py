"""The RCS card carries the stored sender address and subject (never the summary); only a chip tap parses."""

import json

from shared import rcs
from shared.enums import VerdictAnswer

EMAIL_ROW = {"senderAddress": "jimin.kim@jimin-help.example", "subject": "Grandma please help, urgent"}
IMAGE_URL = "https://app.example.com/assets/pidge-icon.png"


def card_content(request_id: str = "req-1") -> dict:
    return rcs.build_rich_card(request_id, EMAIL_ROW, "Jimin", "Grandma", IMAGE_URL)["Content"]["RichCard"]["CardContent"]


def test_card_names_the_sender_address_and_subject_only():
    content = card_content()
    assert "jimin.kim@jimin-help.example" in content["Description"]
    assert "Grandma please help, urgent" in content["Description"]
    assert "summary" not in json.dumps(content).lower()


def test_card_chips_carry_request_id_and_answer_and_fit_the_rcs_limit():
    chips = [suggestion["Reply"] for suggestion in card_content("req-9")["Suggestions"]]
    assert [chip["PostbackData"] for chip in chips] == ["req-9:yes", "req-9:no"]
    assert all(len(chip["Text"]) <= rcs.SUGGESTION_TEXT_MAX_CHARACTERS for chip in chips)


def sns_message(body: object) -> dict:
    return {
        "originationNumber": "+821012345678",
        "destinationNumber": "rcs-a1b2c3d4",
        "messageBody": body if isinstance(body, str) else json.dumps(body),
        "inboundMessageId": "msg-1",
    }


def test_chip_tap_parses_to_request_and_answer():
    reply = rcs.parse_inbound_reply(sns_message({"type": "SUGGESTION", "text": "No, not me", "postbackData": "req-1:no"}))
    assert reply == rcs.RcsReply("req-1", VerdictAnswer.NO, "+821012345678", "msg-1")


def test_typed_text_and_odd_payloads_are_ignored():
    assert rcs.parse_inbound_reply(sns_message("no")) is None
    assert rcs.parse_inbound_reply(sns_message({"type": "SUGGESTION", "postbackData": "req-1:maybe"})) is None
    assert rcs.parse_inbound_reply(sns_message({"type": "LOCATION", "postbackData": "req-1:no"})) is None
    assert rcs.parse_inbound_reply({"originationNumber": "+1"}) is None
    assert rcs.parse_inbound_reply(sns_message({"type": "SUGGESTION", "postbackData": "req-1:undo"})) is None  # PWA only
