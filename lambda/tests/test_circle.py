"""Add someone: the form is checked at the boundary, the row is written PENDING with a server id and a hashed code, the note
carries the code and the new contact's PWA link to the caller's socket only, a PENDING contact is never asked, and only the
code back from the contact's own WhatsApp number (or the labelled demo button) turns the row verified."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from boto3.dynamodb.conditions import Key

from shared import circle, graph
from shared.enums import VerifiedVia
from tests.test_verdict import RECEIVER_ADDRESS, RECEIVER_USER_ID, aws  # noqa: F401
from tests.test_verify_contact import rejection, seed_circle_contact, send_action, verdict_handler  # noqa: F401
from tests.test_whatsapp import inbound_message, sns_message

WHATSAPP_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:whatsapp"
MINSOO_NUMBER = "+821098765432"
PIDGE_NUMBER = "+15550100200"
MINSOO_FORM = {"action": "add_contact", "contactName": " Minsoo  Kim ", "relationship": "son", "whatsappNumber": MINSOO_NUMBER,
               "emailAddress": "Minsoo@Example.com", "extraAddresses": ["minsoo.kim@work.example"]}


@pytest.fixture
def circle_handler(verdict_handler, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CONTACT_VERIFICATION_MOCK", "true")
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("PIDGE_WHATSAPP_NUMBER", PIDGE_NUMBER)
    monkeypatch.setenv("PWA_BASE_URL", "https://app.sendpidge.example")
    handler, pushed_messages = verdict_handler
    user_pushes: list[tuple[str, dict]] = []
    monkeypatch.setattr(handler.channels, "push_to_user",
                        lambda user_id, message: user_pushes.append((user_id, json.loads(handler.channels.encode_push_message(message)))) or 1)
    sent_messages: list[dict] = []
    monkeypatch.setattr(handler.channels, "send_whatsapp_message", lambda message: sent_messages.append(message) or "wamid.OUT")
    return handler, pushed_messages, user_pushes, sent_messages


def added_contact(circle_handler) -> tuple[dict, dict]:
    """Adds Minsoo as the receiver; returns (the contact_added push, the stored row)."""
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    send_action(handler, RECEIVER_USER_ID, MINSOO_FORM)
    [contact_added] = [message for message in pushed_messages if message["type"] == "contact_added"]
    [row] = [row for row in graph.list_contact_rows(RECEIVER_ADDRESS) if row["contactName"] == "Minsoo Kim"]
    return contact_added, row


def whatsapp_text_event(sender_digits: str, text: str, wamid: str = "wamid.CODE") -> dict:
    message = {"from": sender_digits, "id": wamid, "timestamp": "1761267600", "type": "text", "text": {"body": text}}
    return {"Records": [{"Sns": {"TopicArn": WHATSAPP_TOPIC_ARN, "Message": json.dumps(sns_message(inbound_message(message)))}}]}


def test_the_form_is_parsed_strictly():
    form = circle.parse_contact_form(MINSOO_FORM)
    assert (form.contact_name, str(form.relationship), form.whatsapp_number) == ("Minsoo Kim", "son", MINSOO_NUMBER)
    assert form.known_addresses == ["minsoo@example.com", "minsoo.kim@work.example"]
    for broken in ({"contactName": ""}, {"contactName": "x" * 41}, {"relationship": "cousin"}, {"whatsappNumber": "010-9876-5432"},
                   {"whatsappNumber": "+0123456"}, {"emailAddress": "minsoo"}, {"extraAddresses": "a@b.c"},
                   {"extraAddresses": ["a@b.c"] * 6}, {"extraAddresses": ["not an address"]}, {"contactName": 7}):
        with pytest.raises(ValueError):
            circle.parse_contact_form({**MINSOO_FORM, **broken})


def test_add_contact_writes_a_pending_row_with_a_server_id_and_sends_the_note_to_the_caller_only(circle_handler):
    handler, pushed_messages, user_pushes, _sent = circle_handler
    contact_added, row = added_contact(circle_handler)
    assert row["verifiedVia"] == "pending" and row["relationship"] == "son"
    assert row["SK"] == f"CONTACT#{row['contactPwaUserId']}" and len(row["contactPwaUserId"]) == 16
    assert (row["contactWhatsApp"], row["contactEmail"], row["knownAddresses"]) == (MINSOO_NUMBER, "minsoo@example.com",
                                                                                       ["minsoo@example.com", "minsoo.kim@work.example"])
    assert "contactRcs" not in row
    assert row["verificationCodeHash"] == graph.hash_access_token(contact_added["code"]) and len(contact_added["code"]) == 6
    assert contact_added["contact"]["reach"] == {"inapp": True, "rcs": False, "whatsapp": True, "email": True}
    assert MINSOO_NUMBER not in json.dumps(contact_added["contact"]) and "example.com" not in json.dumps(contact_added["contact"])
    note = contact_added["noteText"]
    assert f"Your code is {contact_added['code']}." in note
    assert f"https://wa.me/15550100200?text={contact_added['code']}" in note
    token = note.split("?token=")[1].split()[0]
    assert graph.find_user_id_for_access_token(token) == row["contactPwaUserId"]
    assert graph.get_table().get_item(Key={"PK": f"USER#{row['contactPwaUserId']}", "SK": "PROFILE"})["Item"]["tokenHash"] == graph.hash_access_token(token)
    [(pushed_user, circle_state)] = user_pushes
    assert pushed_user == RECEIVER_USER_ID and circle_state["type"] == "circle_state"
    assert [entry["verifiedVia"] for entry in circle_state["circle"]] == ["pending"]
    assert "noteText" not in json.dumps(circle_state) and token not in json.dumps(circle_state)


def test_without_pidges_number_the_note_has_no_whatsapp_line_and_sync_says_so(circle_handler, monkeypatch):
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    monkeypatch.setenv("PIDGE_WHATSAPP_NUMBER", "")
    contact_added, _row = added_contact(circle_handler)
    assert "wa.me" not in contact_added["noteText"] and "?token=" in contact_added["noteText"]
    send_action(handler, RECEIVER_USER_ID, {"action": "sync"})
    assert pushed_messages[-1]["contactVerification"] == {"whatsapp": False, "mock": True}


def test_the_contact_a_bad_field_a_duplicate_name_and_a_full_circle_write_nothing(circle_handler, monkeypatch):
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    seed_circle_contact()
    send_action(handler, "jimin", MINSOO_FORM)
    send_action(handler, RECEIVER_USER_ID, {**MINSOO_FORM, "whatsappNumber": "0109876"})
    send_action(handler, RECEIVER_USER_ID, {**MINSOO_FORM, "contactName": "jimin"})
    assert pushed_messages == [rejection("not_allowed"), rejection("bad_message"), rejection("duplicate_name")]
    monkeypatch.setattr(circle, "CIRCLE_MAXIMUM_SIZE", 1)
    send_action(handler, RECEIVER_USER_ID, MINSOO_FORM)
    assert pushed_messages[-1] == rejection("circle_full")
    assert [row["SK"] for row in graph.list_contact_rows(RECEIVER_ADDRESS)] == ["CONTACT#jimin"]


def test_a_pending_contact_is_never_asked(circle_handler):
    _contact_added, row = added_contact(circle_handler)
    seed_circle_contact()
    assert [contact.contact_name for contact in graph.list_contacts(RECEIVER_ADDRESS)] == ["Jimin"]
    assert graph.find_contact_among(graph.list_contacts(RECEIVER_ADDRESS), "Minsoo Kim") is None
    graph.mark_contact_verified(RECEIVER_ADDRESS, row["SK"], VerifiedVia.WHATSAPP, graph.now_timestamp(), row["verificationCodeHash"])
    assert graph.find_contact_among(graph.list_contacts(RECEIVER_ADDRESS), "minsoo kim") is not None


def test_the_code_from_the_contacts_own_number_verifies_welcomes_and_pushes_the_circle(circle_handler):
    handler, _pushed, user_pushes, sent_messages = circle_handler
    contact_added, row = added_contact(circle_handler)
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), f" {contact_added['code']} "), None)
    verified_row = graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])
    assert verified_row["verifiedVia"] == "whatsapp" and verified_row["verifiedAt"].endswith("Z")
    assert "verificationCodeHash" not in verified_row and verified_row["contactWhatsApp"] == MINSOO_NUMBER
    assert [entry["verifiedVia"] for entry in user_pushes[-1][1]["circle"]] == ["whatsapp"]
    [welcome] = sent_messages
    assert welcome["to"] == MINSOO_NUMBER and welcome["text"]["body"].startswith("You're in Grandma's circle.")


def test_a_wrong_code_a_strangers_number_an_expired_code_and_a_second_use_write_nothing(circle_handler, monkeypatch):
    handler, _pushed, user_pushes, sent_messages = circle_handler
    contact_added, row = added_contact(circle_handler)
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), "000000"), None)
    handler.handle(whatsapp_text_event("15550001111", contact_added["code"]), None)
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verifiedVia"] == "pending"
    assert sent_messages == [] and len(user_pushes) == 1
    expired = datetime.now(timezone.utc) + circle.VERIFICATION_CODE_LIFETIME + timedelta(minutes=1)
    assert circle.verify_with_code(RECEIVER_ADDRESS, row, contact_added["code"], VerifiedVia.WHATSAPP, expired) is False
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verifiedVia"] == "pending"
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), contact_added["code"]), None)
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), contact_added["code"], "wamid.AGAIN"), None)
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verifiedVia"] == "whatsapp"
    assert len(sent_messages) == 1


def test_the_demo_button_verifies_a_pending_row_with_its_own_code_and_never_a_seeded_one(circle_handler):
    handler, pushed_messages, user_pushes, sent_messages = circle_handler
    seeded_row = seed_circle_contact()
    contact_added, row = added_contact(circle_handler)
    send_action(handler, RECEIVER_USER_ID, {"action": "verify_contact", "contactUserId": "jimin", "code": contact_added["code"]})
    assert pushed_messages[-1] == rejection("not_applied") and graph.get_contact_row(RECEIVER_ADDRESS, "CONTACT#jimin") == seeded_row
    send_action(handler, RECEIVER_USER_ID, {"action": "verify_contact", "contactUserId": row["contactPwaUserId"], "code": contact_added["code"]})
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verifiedVia"] == "mock"
    assert sent_messages == []
    assert sorted(entry["verifiedVia"] for entry in user_pushes[-1][1]["circle"]) == ["mock", "seed"]


def test_adding_a_pending_name_again_re_issues_the_code_and_the_token(circle_handler):
    handler, pushed_messages, user_pushes, _sent = circle_handler
    first_added, first_row = added_contact(circle_handler)
    first_token = first_added["noteText"].split("?token=")[1].split()[0]
    send_action(handler, RECEIVER_USER_ID, {**MINSOO_FORM, "whatsappNumber": "+821000000000"})
    second_added = [message for message in pushed_messages if message["type"] == "contact_added"][-1]
    second_row = graph.get_contact_row(RECEIVER_ADDRESS, first_row["SK"])
    assert second_row["contactPwaUserId"] == first_row["contactPwaUserId"] and second_row["contactWhatsApp"] == "+821000000000"
    assert second_added["code"] != first_added["code"] and second_row["verificationCodeHash"] == graph.hash_access_token(second_added["code"])
    second_token = second_added["noteText"].split("?token=")[1].split()[0]
    assert graph.find_user_id_for_access_token(first_token) is None and graph.find_user_id_for_access_token(second_token) == first_row["contactPwaUserId"]
    assert len(graph.list_contact_rows(RECEIVER_ADDRESS)) == 1 and len(user_pushes) == 2


def test_a_lost_note_push_keeps_the_row_counts_the_loss_and_still_pushes_the_circle(circle_handler, monkeypatch):
    handler, _pushed, user_pushes, _sent = circle_handler
    counted = []
    monkeypatch.setattr(handler.circle_handlers, "emit_count_metric", lambda metric_name, *_: counted.append(metric_name))
    def socket_gone(_connection_id, _message):
        raise RuntimeError("GoneException")
    monkeypatch.setattr(handler.channels, "push_to_connection", socket_gone)
    send_action(handler, RECEIVER_USER_ID, MINSOO_FORM)
    assert [row["verifiedVia"] for row in graph.list_contact_rows(RECEIVER_ADDRESS)] == ["pending"]
    assert counted == ["ContactNotePushFailed"] and [entry["verifiedVia"] for entry in user_pushes[-1][1]["circle"]] == ["pending"]


def test_a_failed_welcome_leaves_the_contact_verified_and_the_circle_pushed(circle_handler, monkeypatch):
    handler, _pushed, user_pushes, _sent = circle_handler
    contact_added, row = added_contact(circle_handler)
    counted = []
    monkeypatch.setattr(handler.circle_handlers, "emit_count_metric", lambda metric_name, *_: counted.append(metric_name))
    def meta_refuses(_message):
        raise RuntimeError("131047 re-engagement")
    monkeypatch.setattr(handler.channels, "send_whatsapp_message", meta_refuses)
    handler.handle(whatsapp_text_event(MINSOO_NUMBER.removeprefix("+"), contact_added["code"]), None)
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verifiedVia"] == "whatsapp"
    assert counted == ["WelcomeSendFailed"] and [entry["verifiedVia"] for entry in user_pushes[-1][1]["circle"]] == ["whatsapp"]


def test_a_stale_row_cannot_verify_twice(circle_handler):
    _handler, _pushed, _user_pushes, _sent = circle_handler
    contact_added, row = added_contact(circle_handler)
    now = datetime.now(timezone.utc)
    assert circle.verify_with_code(RECEIVER_ADDRESS, row, contact_added["code"], VerifiedVia.MOCK, now) is True
    assert circle.verify_with_code(RECEIVER_ADDRESS, row, contact_added["code"], VerifiedVia.WHATSAPP, now) is False  # same stale read
    assert graph.get_contact_row(RECEIVER_ADDRESS, row["SK"])["verifiedVia"] == "mock"


def test_the_circle_entry_says_when_a_pending_code_dies_and_a_bad_relationship_is_not_echoed(circle_handler, caplog):
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    contact_added, row = added_contact(circle_handler)
    assert contact_added["contact"]["codeExpiresAt"] == row["verificationExpiresAt"]
    with caplog.at_level("INFO"):
        send_action(handler, RECEIVER_USER_ID, {**MINSOO_FORM, "contactName": "Other", "relationship": "secret-cousin"})
    assert pushed_messages[-1] == rejection("bad_message") and "secret-cousin" not in caplog.text


def test_an_id_collision_writes_neither_the_row_nor_a_token(circle_handler, monkeypatch):
    handler, pushed_messages, _user_pushes, _sent = circle_handler
    _contact_added, row = added_contact(circle_handler)
    monkeypatch.setattr(circle.secrets, "token_hex", lambda _byte_count: row["contactPwaUserId"])
    token_rows_before = graph.get_table().scan(FilterExpression=Key("PK").begins_with("TOKEN#"))["Count"]
    send_action(handler, RECEIVER_USER_ID, {**MINSOO_FORM, "contactName": "Someone Else"})
    assert pushed_messages[-1] == rejection("not_applied")
    assert [r["contactName"] for r in graph.list_contact_rows(RECEIVER_ADDRESS)] == ["Minsoo Kim"]
    assert graph.get_table().scan(FilterExpression=Key("PK").begins_with("TOKEN#"))["Count"] == token_rows_before
