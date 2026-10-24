"""The WhatsApp template carries the summary and the two payloads; only a quick-reply tap parses."""

import json

from shared import whatsapp
from shared.enums import VerdictAnswer

TEMPLATE_NAME = "pidge_was_this_you"


def test_template_carries_summary_and_both_payloads():
    message = whatsapp.build_template_message("req-1", "Asks for money for a hospital bill.", "+821012345678", TEMPLATE_NAME)
    components = message["template"]["components"]
    assert (message["to"], message["type"], message["template"]["name"]) == ("+821012345678", "template", TEMPLATE_NAME)
    assert components[0] == {"type": "body", "parameters": [{"type": "text", "text": "Asks for money for a hospital bill."}]}
    buttons = [component for component in components if component["type"] == "button"]
    assert [(button["index"], button["parameters"][0]["payload"]) for button in buttons] == [("0", "req-1:yes"), ("1", "req-1:no")]
    assert all(button["sub_type"] == "quick_reply" for button in buttons)


def test_empty_summary_never_sends_an_empty_parameter():
    message = whatsapp.build_template_message("req-1", "   ", "+821012345678", TEMPLATE_NAME)
    assert message["template"]["components"][0]["parameters"][0]["text"] == whatsapp.EMPTY_SUMMARY_TEXT


def sns_message(change_value: dict) -> dict:
    return {
        "context": {"MetaWabaIds": [], "MetaPhoneNumberIds": []},
        "whatsAppWebhookEntry": json.dumps({"id": "974431625101814", "changes": [{"value": change_value, "field": "messages"}]}),
        "aws_account_id": "123456789012",
        "message_timestamp": "2026-10-24T01:00:00Z",
        "messageId": "sns-1",
    }


def inbound_message(message: dict) -> dict:
    return {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "1365540803316739"}, "messages": [message]}


def test_quick_reply_tap_parses_to_request_and_answer_with_plus_added():
    tap = {"from": "821012345678", "id": "wamid.ABC", "timestamp": "1761267600", "type": "button",
           "button": {"payload": "req-1:no", "text": "No"}, "context": {"from": "17753638861", "id": "wamid.TEMPLATE"}}
    reply = whatsapp.parse_inbound_reply(sns_message(inbound_message(tap)))
    assert reply == whatsapp.WhatsAppReply("req-1", VerdictAnswer.NO, "+821012345678", "wamid.ABC")


def test_statuses_text_and_odd_payloads_are_ignored():
    status = {"messaging_product": "whatsapp", "statuses": [{"id": "wamid.X", "status": "delivered", "recipient_id": "821012345678"}]}
    assert whatsapp.parse_inbound_reply(sns_message(status)) is None
    typed_text = {"from": "821012345678", "id": "wamid.T", "type": "text", "text": {"body": "no"}}
    assert whatsapp.parse_inbound_reply(sns_message(inbound_message(typed_text))) is None
    label_only_payload = {"from": "821012345678", "id": "wamid.L", "type": "button", "button": {"payload": "No", "text": "No"}}
    assert whatsapp.parse_inbound_reply(sns_message(inbound_message(label_only_payload))) is None
    wrong_answer = {"from": "821012345678", "id": "wamid.W", "type": "button", "button": {"payload": "req-1:maybe", "text": "No"}}
    assert whatsapp.parse_inbound_reply(sns_message(inbound_message(wrong_answer))) is None
    assert whatsapp.parse_inbound_reply({"whatsAppWebhookEntry": "not json"}) is None
    undo_payload = {"from": "821012345678", "id": "wamid.U", "type": "button", "button": {"payload": "req-1:undo", "text": "No"}}
    assert whatsapp.parse_inbound_reply(sns_message(inbound_message(undo_payload))) is None  # undo only exists in the PWA
