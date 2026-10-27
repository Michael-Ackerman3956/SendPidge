"""One resident can never act for another: Bob, enrolled with his own token, sends every resident action at Grandma's rows
over the real WebSocket handler and is refused with her rows unchanged; Mallory, whose own PROFILE names Grandma's address,
is refused as receiver_mismatch; a stale WACODE# row never blocks another resident's code and is not counted; each
ProfileRejectionReason is counted with its reason."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import pytest

from shared import circle, graph, verdict, whatsapp
from shared.enums import ActionOutcome, Jurisdiction, PaymentRail, UserRole, VerifiedVia
from shared.profiles import PROFILE_REJECTED_METRIC_NAME, UNKNOWN_RECEIVER_METRIC_NAME, ProfileRejectionReason
from tests.helpers.profiles import TEST_ORG_ID, seed_resident
from tests.test_case_report import open_case
from tests.test_verdict import BUCKET_NAME, RECEIVER_ADDRESS, aws  # noqa: F401
from tests.test_verify_contact import MINSOO_FORM, send_action, verdict_handler  # noqa: F401

BOB_ADDRESS = "bob@example.com"
BOB_USER_ID = "bob"
MALLORY_USER_ID = "mallory"
REJECTED_USER_ID = "eve"
GRANDMAS_CONTACT_SORT_KEY = "CONTACT#jimin"
SHARED_WHATSAPP_NUMBER = MINSOO_FORM["whatsappNumbers"][0]
# The USER#eve/PROFILE fields that produce each rejection; None writes no row at all.
PROFILE_FIELDS_FOR_REJECTION = {
    ProfileRejectionReason.NO_PROFILE: None,
    ProfileRejectionReason.UNKNOWN_ROLE: {"role": "superuser"},
    ProfileRejectionReason.NO_RECEIVER_EMAIL: {"role": str(UserRole.RECEIVER)},
    ProfileRejectionReason.NOT_RECEIVER: {"role": str(UserRole.CONTACT)},
    ProfileRejectionReason.RECEIVER_MISMATCH: {"role": str(UserRole.RECEIVER), "receiverEmail": RECEIVER_ADDRESS, "orgId": TEST_ORG_ID},
}


def rejection(request_id: str | None, outcome: ActionOutcome) -> dict:
    return {"type": "action_rejected", "requestId": request_id, "reason": str(outcome)}


def enrol_bob() -> None:
    seed_resident(BOB_ADDRESS, BOB_USER_ID, "Bob")
    circle.issue_access_token(BOB_USER_ID)


def enrol_mallory_naming_grandmas_address() -> None:
    """Mallory's own PROFILE says role receiver and Grandma's address, with a token of her own; RECEIVER#<Grandma's address>
    still names Grandma."""
    graph.get_table().put_item(Item={"PK": f"USER#{MALLORY_USER_ID}", "SK": circle.PROFILE_SORT_KEY,
                                     **PROFILE_FIELDS_FOR_REJECTION[ProfileRejectionReason.RECEIVER_MISMATCH]})
    circle.issue_access_token(MALLORY_USER_ID)


def read_profile_rejection_reasons(capsys) -> list[str]:
    """The reason of every ProfileRejected EMF line printed since the last read."""
    metric_lines = [line for line in capsys.readouterr().out.splitlines() if PROFILE_REJECTED_METRIC_NAME in line]
    return [json.loads(line)["reason"] for line in metric_lines]


def read_grandmas_rows(case_id: str) -> dict:
    case_row = graph.get_case(case_id)
    return {"request": graph.get_request(case_row["requestId"]), "case": case_row,
            "contact": graph.get_contact_row(RECEIVER_ADDRESS, GRANDMAS_CONTACT_SORT_KEY)}


def payment_body(request_id: str) -> dict:
    return {"action": "report_payment", "requestId": request_id, "jurisdiction": "US", "paymentRail": "card", "bank": "Chase",
            "amount": "500", "currency": "USD", "paidAt": "2026-10-24T01:00:00Z", "payeeHint": "ends 4321"}


@pytest.fixture
def mock_bank_configured(verdict_handler, monkeypatch):  # noqa: F811
    """Send to my bank configured, so a refusal comes from the case check, not from a missing mailbox."""
    handler, _pushed_messages = verdict_handler
    monkeypatch.setattr(handler, "MOCK_BANK_MAILBOX", "bank-sim@founder.example")
    monkeypatch.setattr(handler, "CASES_ADDRESS", "cases@sendpidge.example")
    monkeypatch.setenv("CONTACT_VERIFICATION_MOCK", "true")  # verify_contact reaches the contact lookup


def test_another_resident_cannot_act_on_grandmas_rows(verdict_handler, mock_bank_configured):  # noqa: F811
    handler, pushed_messages = verdict_handler
    case_id = open_case()
    enrol_bob()
    rows_before = read_grandmas_rows(case_id)
    request_id = rows_before["request"]["PK"].removeprefix("REQUEST#")
    edit_form = {**MINSOO_FORM, "action": "edit_contact", "contactName": "Jimin", "contactUserId": "jimin"}

    pushed_messages.clear()
    send_action(handler, BOB_USER_ID, {"action": "confirm", "requestId": request_id})
    send_action(handler, BOB_USER_ID, payment_body(request_id))
    send_action(handler, BOB_USER_ID, {"action": "send_report", "caseId": case_id})
    send_action(handler, BOB_USER_ID, {"action": "close_case", "caseId": case_id})
    send_action(handler, BOB_USER_ID, edit_form)
    send_action(handler, BOB_USER_ID, {"action": "verify_contact", "contactUserId": "jimin", "code": "123456"})

    assert pushed_messages == [
        rejection(request_id, ActionOutcome.NOT_ALLOWED), rejection(request_id, ActionOutcome.NOT_ALLOWED),
        rejection(case_id, ActionOutcome.NOT_ALLOWED), rejection(case_id, ActionOutcome.NOT_ALLOWED),
        rejection(None, ActionOutcome.NOT_APPLIED), rejection(None, ActionOutcome.NOT_APPLIED),  # looked up under Bob's own circle
    ]
    assert read_grandmas_rows(case_id) == rows_before

    # Same caller and resident ids, so only the request's receiverEmail check stands between Bob and her request.
    s3_client = boto3.client("s3", region_name="us-east-1")
    confirmed = verdict.confirm_quarantine(request_id, BOB_USER_ID, BOB_USER_ID, BOB_ADDRESS, s3_client, BUCKET_NAME)
    payment = verdict.PaymentReport(request_id, Jurisdiction.US, PaymentRail.CARD, "Chase", Decimal("500"), "USD",
                                    "2026-10-24T01:00:00Z", "ends 4321")
    reported = verdict.report_payment(payment, BOB_USER_ID, BOB_USER_ID, BOB_ADDRESS)
    assert (confirmed.outcome, reported.outcome) == (ActionOutcome.NOT_ALLOWED, ActionOutcome.NOT_ALLOWED)
    assert read_grandmas_rows(case_id) == rows_before


def test_a_profile_naming_grandmas_address_is_refused_as_receiver_mismatch(verdict_handler, mock_bank_configured, capsys):  # noqa: F811
    """Only the constant-time receiverUserId compare stands between Mallory and Grandma's rows: without it her sync gets
    Grandma's state and her close_case closes Grandma's case."""
    handler, pushed_messages = verdict_handler
    case_id = open_case()
    enrol_mallory_naming_grandmas_address()
    rows_before = read_grandmas_rows(case_id)
    pushed_messages.clear()

    for action_body in ({"action": "sync"}, {"action": "close_case", "caseId": case_id}):
        capsys.readouterr()
        send_action(handler, MALLORY_USER_ID, action_body)
        assert read_profile_rejection_reasons(capsys) == [ProfileRejectionReason.RECEIVER_MISMATCH]

    assert pushed_messages == [rejection(None, ActionOutcome.NOT_ALLOWED), rejection(None, ActionOutcome.NOT_ALLOWED)]
    assert read_grandmas_rows(case_id) == rows_before


def test_a_stale_code_row_never_blocks_another_resident(verdict_handler, monkeypatch, capsys):  # noqa: F811
    handler, _pushed_messages = verdict_handler
    enrol_bob()
    form = circle.parse_contact_form(MINSOO_FORM)
    now = datetime.now(timezone.utc)
    bobs_enrolment = circle.enrol_contact(BOB_ADDRESS, form, now)
    grandmas_enrolment = circle.enrol_contact(RECEIVER_ADDRESS, form, now)
    assert (bobs_enrolment.outcome, grandmas_enrolment.outcome) == (ActionOutcome.RECORDED, ActionOutcome.RECORDED)
    graph.get_table().delete_item(Key={"PK": f"RECEIVER#{BOB_ADDRESS}", "SK": circle.PROFILE_SORT_KEY})  # as unenrol does
    # The stale row is tried first, so before the fix its raise ended the loop before Grandma's row.
    assert circle.list_receivers_waiting_on_number(SHARED_WHATSAPP_NUMBER) == [BOB_ADDRESS, RECEIVER_ADDRESS]
    sent_messages: list[dict] = []

    def record_whatsapp_message(message: dict) -> str:
        sent_messages.append(message)
        return "wamid.welcome"

    monkeypatch.setattr(handler.channels, "send_whatsapp_message", record_whatsapp_message)
    capsys.readouterr()

    handler.circle_handlers.handle_whatsapp_text(whatsapp.InboundText(grandmas_enrolment.code, SHARED_WHATSAPP_NUMBER, "wamid.code"))

    verified_row = graph.get_contact_row(RECEIVER_ADDRESS, grandmas_enrolment.contact_row["SK"])
    assert verified_row["verifiedVia"] == VerifiedVia.WHATSAPP
    assert len(sent_messages) == 1  # the welcome, to the number that sent the code
    assert not any(UNKNOWN_RECEIVER_METRIC_NAME in line for line in capsys.readouterr().out.splitlines())  # a stale row pages nobody


@pytest.mark.parametrize("rejection_reason", list(ProfileRejectionReason))
def test_each_rejected_profile_is_refused_and_counted_with_its_reason(verdict_handler, capsys, rejection_reason):  # noqa: F811
    """A resident action runs require_resident_caller, the one path that can reach every reason."""
    handler, pushed_messages = verdict_handler
    profile_fields = PROFILE_FIELDS_FOR_REJECTION[rejection_reason]
    if profile_fields is not None:
        graph.get_table().put_item(Item={"PK": f"USER#{REJECTED_USER_ID}", "SK": circle.PROFILE_SORT_KEY,
                                         "tokenHash": f"{REJECTED_USER_ID}-token-hash", **profile_fields})
    capsys.readouterr()

    send_action(handler, REJECTED_USER_ID, {"action": "close_case", "caseId": "case-any"})

    assert pushed_messages == [rejection(None, ActionOutcome.NOT_ALLOWED)]
    assert read_profile_rejection_reasons(capsys) == [rejection_reason]
