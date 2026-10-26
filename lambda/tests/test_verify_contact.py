"""Onboarding's code step: the receiver only, mock mode only, the contact looked up under her own partition, the right code
updates verifiedVia / verifiedAt in place (numbers kept), and every refusal writes nothing."""

import importlib
import json

import pytest

from shared import graph
from tests.test_verdict import BUCKET_NAME, CONTACT_EMAIL, JIMIN_WHATSAPP_NUMBER, RECEIVER_ADDRESS, RECEIVER_USER_ID, aws  # noqa: F401

CONTACT_SORT_KEY = "CONTACT#jimin"
OTHER_RECEIVER_ADDRESS = "someone-else@example.com"
MOCK_CODE = "4821"  # the handler's only code: the PWA plays the reply with it


@pytest.fixture
def verdict_handler(aws, monkeypatch):  # noqa: F811
    """pidge-verdict as deployed (env names from the CDK stack); pushes to the caller's socket are captured as wire JSON."""
    monkeypatch.setenv("RECEIVER_ADDRESS", RECEIVER_ADDRESS)
    monkeypatch.setenv("RECEIVER_USER_ID", RECEIVER_USER_ID)
    monkeypatch.setenv("RECEIVER_NAME", "Grandma")
    monkeypatch.setenv("EMAIL_BUCKET", BUCKET_NAME)
    monkeypatch.setenv("RECEIPT_SENDER", "pidge@sendpidge.example")
    monkeypatch.setenv("RCS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:rcs")
    monkeypatch.setenv("WHATSAPP_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:whatsapp")
    monkeypatch.setenv("WEBSOCKET_ENDPOINT", "https://example.com")
    handler = importlib.import_module("pidge_verdict.handler")
    importlib.reload(handler)
    pushed_messages: list[dict] = []
    monkeypatch.setattr(handler.channels, "push_to_connection",
                        lambda _connection_id, message: pushed_messages.append(json.loads(handler.channels.encode_push_message(message))))
    return handler, pushed_messages


def seed_circle_contact(receiver_address: str = RECEIVER_ADDRESS, contact_user_id: str = "jimin") -> dict:
    """The seed_graph.py row shape, with a WhatsApp number and no RCS number."""
    contact_row = {
        "PK": f"RECEIVER#{receiver_address}", "SK": f"CONTACT#{contact_user_id}", "contactName": contact_user_id.title(),
        "contactPwaUserId": contact_user_id, "knownAddresses": [f"{contact_user_id}@example.com"], "verifiedVia": "seed",
        "contactWhatsApp": JIMIN_WHATSAPP_NUMBER, "contactEmail": CONTACT_EMAIL,
    }
    graph.get_table().put_item(Item=contact_row)
    return contact_row


def stored_contact_row(receiver_address: str = RECEIVER_ADDRESS, sort_key: str = CONTACT_SORT_KEY) -> dict:
    return graph.get_contact_row(receiver_address, sort_key)


def send_action(handler, user_id: str, body: dict) -> None:
    connection_id = f"conn-{user_id}"
    graph.register_connection(connection_id, user_id)
    handler.handle({"requestContext": {"connectionId": connection_id, "messageId": "ws-1"}, "body": json.dumps(body)}, None)


def rejection(reason: str) -> dict:
    return {"type": "action_rejected", "requestId": None, "reason": reason}


@pytest.fixture
def mock_on(monkeypatch):
    monkeypatch.setenv("CONTACT_VERIFICATION_MOCK", "true")


def test_the_right_code_marks_the_contact_verified_in_place_and_re_sends_the_circle(verdict_handler, mock_on):
    handler, pushed_messages = verdict_handler
    seeded_row = seed_circle_contact()
    send_action(handler, RECEIVER_USER_ID, {"action": "verify_contact", "contactUserId": "jimin", "code": MOCK_CODE})
    verified_row = stored_contact_row()
    assert verified_row["verifiedVia"] == "mock"
    assert verified_row["verifiedAt"].endswith("Z")
    unchanged_fields = ("contactWhatsApp", "knownAddresses", "contactEmail", "contactName", "contactPwaUserId")
    assert {field: verified_row[field] for field in unchanged_fields} == {field: seeded_row[field] for field in unchanged_fields}
    [sync_message] = pushed_messages
    assert (sync_message["type"], sync_message["role"], sync_message["receiverName"]) == ("sync_state", "receiver", "Grandma")
    assert sync_message["circle"] == [{"contactUserId": "jimin", "contactName": "Jimin", "verifiedVia": "mock",
                                       "verifiedAt": verified_row["verifiedAt"],
                                       "reach": {"inapp": True, "rcs": False, "whatsapp": True}}]


def test_a_wrong_code_writes_nothing_and_is_not_applied(verdict_handler, mock_on):
    handler, pushed_messages = verdict_handler
    seeded_row = seed_circle_contact()
    send_action(handler, RECEIVER_USER_ID, {"action": "verify_contact", "contactUserId": "jimin", "code": "0000"})
    assert stored_contact_row() == seeded_row
    assert pushed_messages == [rejection("not_applied")]


def test_the_contact_cannot_verify_even_with_the_right_code(verdict_handler, mock_on):
    handler, pushed_messages = verdict_handler
    seeded_row = seed_circle_contact()
    send_action(handler, "jimin", {"action": "verify_contact", "contactUserId": "jimin", "code": MOCK_CODE})
    assert stored_contact_row() == seeded_row
    assert pushed_messages == [rejection("not_allowed")]


def test_mock_mode_off_refuses_the_right_code(verdict_handler):
    handler, pushed_messages = verdict_handler
    seeded_row = seed_circle_contact()
    send_action(handler, RECEIVER_USER_ID, {"action": "verify_contact", "contactUserId": "jimin", "code": MOCK_CODE})
    assert stored_contact_row() == seeded_row
    assert pushed_messages == [rejection("not_allowed")]


def test_a_contact_outside_the_receivers_own_partition_is_not_applied(verdict_handler, mock_on):
    handler, pushed_messages = verdict_handler
    seed_circle_contact()
    other_receivers_row = seed_circle_contact(OTHER_RECEIVER_ADDRESS, "minsu")
    send_action(handler, RECEIVER_USER_ID, {"action": "verify_contact", "contactUserId": "minsu", "code": MOCK_CODE})
    assert stored_contact_row(OTHER_RECEIVER_ADDRESS, "CONTACT#minsu") == other_receivers_row
    assert [row["SK"] for row in graph.list_contact_rows(RECEIVER_ADDRESS)] == [CONTACT_SORT_KEY]
    assert pushed_messages == [rejection("not_applied")]


@pytest.mark.parametrize("fields", [{"contactUserId": "jimin", "code": 4821}, {"code": MOCK_CODE}])
def test_a_non_string_code_or_missing_contact_id_is_a_bad_message(verdict_handler, mock_on, fields):
    handler, pushed_messages = verdict_handler
    seeded_row = seed_circle_contact()
    send_action(handler, RECEIVER_USER_ID, {"action": "verify_contact", **fields})
    assert stored_contact_row() == seeded_row
    assert pushed_messages == [rejection("bad_message")]


def test_a_seeded_row_without_verified_via_reads_as_seed_and_warns(caplog):
    from shared import payloads
    row = {"PK": "RECEIVER#grandma@example.com", "SK": "CONTACT#jimin", "contactName": "Jimin", "contactPwaUserId": "jimin"}
    with caplog.at_level("WARNING"):
        entry = payloads.circle_entry_for_receiver(row)
    assert entry["verifiedVia"] == "seed"
    assert "no verifiedVia" in caplog.text
    with pytest.raises(ValueError):
        payloads.circle_entry_for_receiver({**row, "verifiedVia": "carrier_pigeon"})
