"""The WhatsApp template carries the summary and the two payloads; only a quick-reply tap parses."""

import json

import pytest

from shared import whatsapp
from shared.enums import CallPromise, VerdictAnswer

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


def test_call_offer_is_interactive_with_two_reply_ids_and_no_email_text():
    no_offer = whatsapp.build_call_offer_message("req-1", "Grandma", "+821012345678", VerdictAnswer.NO)
    yes_offer = whatsapp.build_call_offer_message("req-1", "Grandma", "+821012345678", VerdictAnswer.YES)
    assert (no_offer["messaging_product"], no_offer["to"], no_offer["type"]) == ("whatsapp", "+821012345678", "interactive")
    assert no_offer["interactive"]["type"] == "button"
    buttons = no_offer["interactive"]["action"]["buttons"]
    assert [button["reply"]["id"] for button in buttons] == ["call:req-1:today", "call:req-1:tomorrow"]
    assert all(button["type"] == "reply" and len(button["reply"]["title"]) <= 20 for button in buttons)
    no_body, yes_body = no_offer["interactive"]["body"]["text"], yes_offer["interactive"]["body"]["text"]
    assert "Grandma" in no_body and "Grandma" in yes_body and no_body != yes_body
    assert not any(character.isdigit() for character in no_body + yes_body)
    for other_answer in (VerdictAnswer.UNDO, VerdictAnswer.REVERSE):
        with pytest.raises(ValueError):
            whatsapp.build_call_offer_message("req-1", "Grandma", "+821012345678", other_answer)
    reminder = whatsapp.build_call_reminder_message("Grandma", "+821012345678")
    assert reminder["type"] == "text" and reminder["text"]["body"] == "Reminder: you said you'd call Grandma today."


def button_reply_tap(button_id: str) -> dict:
    return {"from": "821012345678", "id": "wamid.CALL", "timestamp": "1761267600", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "Tomorrow"}},
            "context": {"from": "17753638861", "id": "wamid.OFFER"}}


def test_interactive_button_reply_parses_to_a_call_promise_with_plus_added():
    reply = whatsapp.parse_call_promise_reply(sns_message(inbound_message(button_reply_tap("call:req-1:tomorrow"))))
    assert reply == whatsapp.CallPromiseReply("req-1", CallPromise.TOMORROW, "+821012345678", "wamid.CALL")
    assert whatsapp.parse_inbound_reply(sns_message(inbound_message(button_reply_tap("call:req-1:tomorrow")))) is None


def test_no_acknowledgement_carries_the_reverse_button_and_no_email_text():
    message = whatsapp.build_no_acknowledgement_message("req-1", "+821012345678")
    assert (message["to"], message["type"], message["interactive"]["type"]) == ("+821012345678", "interactive", "button")
    assert message["interactive"]["body"]["text"] == "Noted. The email stays held."
    [button] = message["interactive"]["action"]["buttons"]
    assert button == {"type": "reply", "reply": {"id": "reverse:req-1", "title": "I made a mistake"}}
    assert len(button["reply"]["title"]) <= 20


def test_reverse_acknowledgement_is_plain_text_with_the_receiver_name():
    message = whatsapp.build_reverse_acknowledgement_message("Grandma", "+821012345678")
    assert (message["to"], message["type"]) == ("+821012345678", "text")
    assert message["text"]["body"] == "Noted. You did send it, so the email goes through to Grandma."


def test_reverse_button_tap_parses_to_a_reverse_answer():
    reply = whatsapp.parse_inbound_reply(sns_message(inbound_message(button_reply_tap("reverse:req-1"))))
    assert reply == whatsapp.WhatsAppReply("req-1", VerdictAnswer.REVERSE, "+821012345678", "wamid.CALL")


def test_call_offer_taps_and_odd_reply_ids_are_not_answers():
    for foreign_id in ("call:req-1:tomorrow", "reverse:", "reverse", "undo:req-1", "req-1:no", "reverse:req-1:extra"):
        assert whatsapp.parse_inbound_reply(sns_message(inbound_message(button_reply_tap(foreign_id)))) is None
    list_reply = {"from": "821012345678", "id": "wamid.LIST", "type": "interactive",
                  "interactive": {"type": "list_reply", "list_reply": {"id": "reverse:req-1", "title": "x"}}}
    assert whatsapp.parse_inbound_reply(sns_message(inbound_message(list_reply))) is None


def test_template_taps_text_and_foreign_ids_are_not_call_promises():
    template_tap = {"from": "821012345678", "id": "wamid.ABC", "type": "button", "button": {"payload": "req-1:no", "text": "No"}}
    assert whatsapp.parse_call_promise_reply(sns_message(inbound_message(template_tap))) is None
    typed_text = {"from": "821012345678", "id": "wamid.T", "type": "text", "text": {"body": "today"}}
    assert whatsapp.parse_call_promise_reply(sns_message(inbound_message(typed_text))) is None
    for foreign_id in ("bad:req:today", "call:req:soon", "call:req:today:extra", "call::today", "reverse:req-1"):
        assert whatsapp.parse_call_promise_reply(sns_message(inbound_message(button_reply_tap(foreign_id)))) is None
    list_reply = {**button_reply_tap("call:req-1:today"), "interactive": {"type": "list_reply", "list_reply": {"id": "call:req-1:today"}}}
    assert whatsapp.parse_call_promise_reply(sns_message(inbound_message(list_reply))) is None
    assert whatsapp.parse_call_promise_reply({"whatsAppWebhookEntry": "not json"}) is None


def test_failed_status_carries_the_meta_error_code():
    failed = {"messaging_product": "whatsapp", "statuses": [{"id": "wamid.OFFER", "status": "failed", "recipient_id": "821012345678",
                                                             "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}
    assert whatsapp.parse_failed_status(sns_message(failed)) == whatsapp.FailedWhatsAppStatus("wamid.OFFER", "131047")
    delivered = {"messaging_product": "whatsapp", "statuses": [{"id": "wamid.OFFER", "status": "delivered", "recipient_id": "821012345678"}]}
    assert whatsapp.parse_failed_status(sns_message(delivered)) is None
    assert whatsapp.parse_failed_status(sns_message(inbound_message(button_reply_tap("call:req-1:today")))) is None
