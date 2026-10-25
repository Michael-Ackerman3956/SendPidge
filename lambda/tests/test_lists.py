"""The scam list: normalisers, lookalikes, LIST# reads that add reasons (never a verdict), a failed read that marks, and the
ask cap that fails closed. Reads go through the production table fixture."""

import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from shared import graph, lists
from shared.enums import Friction, IndicatorKind, ListSource, PolicyDecision, ReasonCode, RequestState, WarnReason
from shared.lists import (
    DynamoListStore, ListLookupFailed, SeedListStore, confusable_skeleton, is_lookalike_name,
    list_row_from_seed, lookup_sender_reasons, normalise_phone_e164, normalise_registrable_domain, parse_list_row,
    sender_reasons_or_lookup_failure,
)
from shared.policy import decide
from shared.reader_agent import EmailVerdict
from shared.enums import Intent
from tests.test_verdict import aws  # noqa: F401

SEED_PATH = Path(__file__).resolve().parents[2] / "infra" / "seed" / "lists.sample.json"
JIMIN = graph.Contact("CONTACT#jimin", "Jimin", "jimin", known_addresses=("jimin@example.com",))
MINHO = graph.Contact("CONTACT#minho", "Minho", "minho", known_addresses=("minho@family.example",))
CONTACTS = [JIMIN, MINHO]


def seed_rows() -> list[dict]:
    return [list_row_from_seed(seed) for seed in json.loads(SEED_PATH.read_text())]


def seeded_store() -> SeedListStore:
    return SeedListStore(seed_rows())


def test_normalisers():
    assert normalise_registrable_domain("Mail.Jimin-Help.Example.") == "jimin-help.example"
    assert normalise_registrable_domain("news.bbc.co.uk") == "bbc.co.uk"
    assert normalise_phone_e164("+82 10-0000-0001") == "+821000000001"
    assert normalise_phone_e164("0082 10 0000 0001") == "+821000000001"
    with pytest.raises(ValueError):
        normalise_phone_e164("call me")


def test_skeleton_and_lookalike_names():
    assert confusable_skeleton("Jіmin") == confusable_skeleton("Jimin")  # Cyrillic і
    assert confusable_skeleton("PayPa1.com") == "paypal.com"
    assert is_lookalike_name("Jimln", "Jimin")       # one edit
    assert is_lookalike_name("Jіmin", "Jimin")       # confusable
    assert is_lookalike_name("J i m i n", "Jimin")   # spacing
    assert not is_lookalike_name("JImin", "Jimin")   # exact once case-folded: gate 1 owns it
    assert not is_lookalike_name("Jim", "Kim")       # short names are never one-edit lookalikes
    assert not is_lookalike_name("Grandpa", "Jimin")


def test_every_seed_row_parses_and_has_the_full_shape():
    rows = seed_rows()
    assert len(rows) >= 20
    for row in rows:
        parsed = parse_list_row(row)
        assert parsed.source is ListSource.SAMPLE
        assert {"category", "strength", "source", "tier", "reason", "attribution", "addedAt", "expiresAt", "generation"} <= set(row)
    assert {row["PK"] for row in rows} == {"LIST#domain", "LIST#address", "LIST#lookalike", "LIST#phone", "LIST#url"}
    assert any(row["SK"] == "+821000000001" for row in rows if row["PK"] == "LIST#phone")


@pytest.mark.parametrize(
    ("sender_name", "sender_address", "expected_reasons"),
    [
        ("Jimin", "jimin.kim@jimin-help.example", [ReasonCode.LISTED_SENDER_DOMAIN]),
        ("Grandma's Bank", "support@kakaobank-secure.example", [ReasonCode.LISTED_SENDER_ADDRESS, ReasonCode.LISTED_SENDER_DOMAIN]),
        ("Jimln", "someone@freemail.example", [ReasonCode.LOOKALIKE_SENDER_NAME]),
        ("Jіmin", "someone@freemail.example", [ReasonCode.LOOKALIKE_SENDER_NAME]),
        ("Jimln", "jimin@example.com", []),  # a near-match from the contact's own known address is not a lookalike
        ("Jimin", "jimin@example.com", []),  # the real thing
        ("Minho", "minho@family-example.example", [ReasonCode.LOOKALIKE_SENDER_DOMAIN]),
        ("Newsletter", "news@shop.example", []),
    ],
)
def test_lookup_turns_rows_and_near_matches_into_reasons(sender_name, sender_address, expected_reasons):
    domain = sender_address.rpartition("@")[2]
    assert lookup_sender_reasons(seeded_store(), sender_address, domain, sender_name, CONTACTS) == expected_reasons


def test_a_confirm_row_is_reported_sender_and_a_read_failure_marks(aws, monkeypatch):  # noqa: F811
    graph.write_listed_address("Scammer@Lookalike.example", "Reported after the real contact said they did not send it")
    stored = graph.get_table().get_item(Key={"PK": "LIST#address", "SK": "scammer@lookalike.example"})["Item"]
    assert parse_list_row(stored).source is ListSource.PIDGE_CONFIRMED and stored["tier"] == 0
    store = DynamoListStore()
    assert lookup_sender_reasons(store, "scammer@lookalike.example", "lookalike.example", "Someone", CONTACTS) == [ReasonCode.REPORTED_SENDER]
    graph.delete_listed_address("scammer@lookalike.example")
    assert lookup_sender_reasons(store, "scammer@lookalike.example", "lookalike.example", "Someone", CONTACTS) == []

    def failing_get_item(**_):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}, "GetItem")

    monkeypatch.setattr(graph.get_table(), "get_item", failing_get_item)
    with pytest.raises(ListLookupFailed):
        lookup_sender_reasons(store, "x@y.example", "y.example", "X", CONTACTS)
    assert sender_reasons_or_lookup_failure(store, "x@y.example", "y.example", "X", CONTACTS) == [ReasonCode.LIST_LOOKUP_FAILED]


def test_list_reasons_mark_but_never_hold_on_their_own():
    quiet_verdict = EmailVerdict(claimed_person=None, intent=Intent.NONE, confidence=0.1, summary="A note.")
    marked = decide(quiet_verdict, None, None, "support@kakaobank-secure.example", [ReasonCode.LISTED_SENDER_ADDRESS])
    assert (marked.decision, marked.warn_reason) == (PolicyDecision.WARN, WarnReason.LISTED_SENDER)
    failed = decide(quiet_verdict, None, None, "x@y.example", [ReasonCode.LIST_LOOKUP_FAILED])
    assert (failed.decision, failed.warn_reason) == (PolicyDecision.WARN, WarnReason.LISTED_SENDER)
    clean = decide(quiet_verdict, None, None, "x@y.example", [])
    assert clean.decision is PolicyDecision.PASS


def test_the_ask_cap_holds_the_fourth_ask_and_fails_closed(aws, monkeypatch):  # noqa: F811
    for _ in range(3):
        assert graph.take_ask_slot("CONTACT#jimin", 3)
    assert not graph.take_ask_slot("CONTACT#jimin", 3)
    assert graph.take_ask_slot("CONTACT#minho", 3)  # the cap is per contact

    def failing_update_item(**_):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}, "UpdateItem")

    monkeypatch.setattr(graph.get_table(), "update_item", failing_update_item)
    assert not graph.take_ask_slot("CONTACT#other", 3)


def test_record_ask_capped_leaves_the_request_held_with_nothing_sent(aws):  # noqa: F811
    from tests.test_verdict import new_request

    request_id = new_request()
    graph.record_ask_capped(request_id)
    request_row = graph.get_request(request_id)
    assert (request_row["channelsSent"], request_row["askCapped"], request_row["status"]) == ([], True, "pending")


def test_seed_store_and_dynamo_store_read_the_same_row(aws):  # noqa: F811
    row = next(row for row in seed_rows() if row["PK"] == "LIST#domain")
    graph.get_table().put_item(Item=row)
    assert DynamoListStore().get_row(IndicatorKind.DOMAIN, row["SK"]) == seeded_store().get_row(IndicatorKind.DOMAIN, row["SK"])


@pytest.fixture
def reader_handler(aws, monkeypatch):  # noqa: F811
    """pidge_reader.handler with the Reader never called and every push recorded; the table is moto's."""
    import importlib

    for name, value in {"RECEIVER_ADDRESS": "grandma@sendpidge.example", "RECEIVER_USER_ID": "grandma", "RECEIVER_NAME": "Grandma",
                        "READER_MODEL": "haiku-4.5", "ESCALATION_MODEL": "opus-4.5", "ASKS_PER_CONTACT_PER_HOUR": "3"}.items():
        monkeypatch.setenv(name, value)
    handler = importlib.import_module("pidge_reader.handler")
    importlib.reload(handler)
    pushed = []
    monkeypatch.setattr(handler.channels, "push_to_user", lambda user_id, message: pushed.append(message["email"]) or 1)
    return handler, pushed


def held_outcome(contact):
    from shared.policy import PolicyOutcome
    return PolicyOutcome(PolicyDecision.HOLD, None, (ReasonCode.UNKNOWN_ADDRESS,), contact=contact, friction=Friction.DOUBLE_TAP)


def test_a_capped_ask_is_stored_as_the_same_warn_the_receiver_saw(reader_handler, monkeypatch):
    """A reload must show the calm banner, not a countdown for an ask nobody received."""
    from tests.test_verdict import EMAIL_ID, VERDICT_FIELDS, seed_contact

    handler, pushed = reader_handler
    contact = seed_contact()
    email_row = {"s3Key": EMAIL_ID, "senderName": "Jimin", "senderAddress": "x@y.example", "subject": "Hi", "receivedAt": "2026-10-25T00:00:00Z",
                 "textBody": "Send money", "quarantined": False}
    graph.claim_email(EMAIL_ID, email_row)
    for _ in range(3):
        assert graph.take_ask_slot(contact.contact_key, 3)
    asked = []
    monkeypatch.setattr(handler.channels, "ask_on_every_channel", lambda *_: asked.append(True))
    verdict = EmailVerdict(**VERDICT_FIELDS)
    handler.hold_and_ask(EMAIL_ID, email_row, verdict, held_outcome(contact), None)

    assert asked == []
    stored = graph.get_email(EMAIL_ID)
    assert (stored["state"], stored["warnReason"]) == (str(PolicyDecision.WARN), str(WarnReason.ASK_CAPPED))
    request_row = graph.get_request(stored["requestId"])
    assert (request_row["status"], request_row["askCapped"]) == (str(RequestState.PENDING), True)
    assert [(email["state"], email["warnReason"]) for email in pushed] == [("hold", None), ("warn", "ask_capped")]
    assert pushed[-1]["request"]["requestId"] == stored["requestId"]
