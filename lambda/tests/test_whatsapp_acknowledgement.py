"""pidge-verdict answers a WhatsApp tap on WhatsApp: "Noted" with the reverse button after a No, plain text after a reverse,
nothing for a duplicate, a stranger's tap or an in-app No."""

import json

import pytest

from shared import graph
from shared.enums import VerdictAnswer
from tests.test_verdict import JIMIN_WHATSAPP_NUMBER, aws, new_request  # noqa: F401
from tests.test_verify_contact import verdict_handler  # noqa: F401
from tests.test_whatsapp import button_reply_tap, inbound_message, sns_message

WHATSAPP_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:whatsapp"
LISTED_SENDER = "scammer@lookalike.example"  # new_request's sender, as write_listed_address lowers it


@pytest.fixture
def acknowledging_handler(verdict_handler, monkeypatch):  # noqa: F811
    handler, _pushed_messages = verdict_handler
    sent_messages: list[dict] = []
    monkeypatch.setattr(handler.channels, "send_whatsapp_message", lambda message: sent_messages.append(message) or "wamid.OUT")
    return handler, sent_messages


def whatsapp_event(message: dict) -> dict:
    return {"Records": [{"Sns": {"TopicArn": WHATSAPP_TOPIC_ARN, "Message": json.dumps(sns_message(inbound_message(message)))}}]}


def template_no_tap(request_id: str, wamid: str = "wamid.NO") -> dict:
    return {"from": JIMIN_WHATSAPP_NUMBER.removeprefix("+"), "id": wamid, "type": "button",
            "button": {"payload": f"{request_id}:no", "text": "No"}}


def reverse_tap(request_id: str, sender_digits: str = JIMIN_WHATSAPP_NUMBER.removeprefix("+")) -> dict:
    tap = button_reply_tap(f"reverse:{request_id}")
    return {**tap, "from": sender_digits, "id": "wamid.REVERSE"}


def status_of(request_id: str) -> str:
    return graph.get_request(request_id)["status"]


def listed_row_exists() -> bool:
    return "Item" in graph.get_table().get_item(Key={"PK": "LIST#address", "SK": LISTED_SENDER})


def test_a_no_tap_is_acknowledged_once_with_the_reverse_button(acknowledging_handler):
    handler, sent_messages = acknowledging_handler
    request_id = new_request()
    handler.handle(whatsapp_event(template_no_tap(request_id)), None)
    handler.handle(whatsapp_event(template_no_tap(request_id)), None)  # SNS redelivery of the same wamid
    assert status_of(request_id) == "denied"
    [acknowledgement] = sent_messages
    assert (acknowledgement["to"], acknowledgement["type"]) == (JIMIN_WHATSAPP_NUMBER, "interactive")
    assert acknowledgement["interactive"]["body"]["text"] == "Noted. The email stays held."
    assert acknowledgement["interactive"]["action"]["buttons"][0]["reply"]["id"] == f"reverse:{request_id}"


def test_the_reverse_button_takes_the_no_back_drops_the_list_row_and_is_acknowledged(acknowledging_handler):
    handler, sent_messages = acknowledging_handler
    request_id = new_request()
    handler.handle(whatsapp_event(template_no_tap(request_id)), None)
    graph.write_listed_address(LISTED_SENDER, "Reported after the real contact said they did not send it", request_id)
    handler.handle(whatsapp_event(reverse_tap(request_id)), None)
    assert status_of(request_id) == "reversed_no"
    assert not listed_row_exists()
    assert [message["type"] for message in sent_messages] == ["interactive", "text"]
    assert sent_messages[1]["text"]["body"] == "Noted. You did send it, so the email goes through to Grandma."


def test_a_reverse_from_another_number_is_ignored_and_not_acknowledged(acknowledging_handler):
    handler, sent_messages = acknowledging_handler
    request_id = new_request()
    handler.handle(whatsapp_event(template_no_tap(request_id)), None)
    handler.handle(whatsapp_event(reverse_tap(request_id, sender_digits="821099999999")), None)
    assert status_of(request_id) == "denied"
    assert len(sent_messages) == 1


def test_an_in_app_no_sends_nothing_on_whatsapp(acknowledging_handler):
    handler, sent_messages = acknowledging_handler
    request_id = new_request()
    handler.handle_answer("conn-jimin", "jimin", {"requestId": request_id}, "ws-1", VerdictAnswer.NO)
    assert status_of(request_id) == "denied"
    assert sent_messages == []


def test_a_failed_acknowledgement_never_undoes_the_answer(acknowledging_handler, monkeypatch, caplog):
    handler, _sent_messages = acknowledging_handler
    request_id = new_request()

    def refuse(_message):
        raise RuntimeError("Meta error 131047")

    monkeypatch.setattr(handler.channels, "send_whatsapp_message", refuse)
    handler.handle(whatsapp_event(template_no_tap(request_id)), None)
    assert status_of(request_id) == "denied"
    assert "whatsapp acknowledgement for" in caplog.text
