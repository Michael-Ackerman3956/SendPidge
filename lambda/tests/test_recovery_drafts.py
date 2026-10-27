"""The three drafts come from the case row only, the pack email goes to her own mailbox only, and a refused send leaves the
note unstamped but still pushed."""

import importlib
from decimal import Decimal

import boto3
import pytest
from botocore.stub import ANY, Stubber

from shared import graph, recovery_drafts
from tests.test_verdict import RECEIVER_USER_ID, aws  # noqa: F401

CASE_ROW = {"PK": "CASE#case-1", "paymentRail": "zelle", "jurisdiction": "US", "bank": "Chase", "amount": Decimal("500"),
            "currency": "USD", "paidAt": "2026-10-20T09:00:00Z", "payeeHint": "a Kakao account"}
RECEIVER_MAILBOX = "grandma@example.com"
PIDGE_ADDRESS = "pidge@sendpidge.example"


def test_drafts_carry_the_case_facts_and_nothing_else():
    drafts = recovery_drafts.build_recovery_drafts(CASE_ROW, "2026-10-27")
    assert set(drafts) == {"callScript", "disputeLetter", "policeReport"}
    for draft in drafts.values():
        assert "Chase" in draft and "500.00 USD" in draft and "2026-10-20" in draft and "Zelle transfer" in draft
        assert "to a Kakao account" in draft
    assert "Date: 2026-10-27" in drafts["disputeLetter"]
    assert "Call Chase" in drafts["callScript"]
    assert drafts["policeReport"].startswith("What happened, in order:")


def test_an_empty_payee_hint_leaves_no_dangling_clause():
    drafts = recovery_drafts.build_recovery_drafts({**CASE_ROW, "payeeHint": ""}, "2026-10-27")
    assert " to " not in drafts["policeReport"].split("\n")[3]


def test_pack_email_lists_the_steps_in_order_then_the_drafts():
    pack = {"steps": [{"order": 2, "action": "Report to", "contact": "FTC", "deadlineDays": 30},
                      {"order": 1, "action": "Call", "contact": "Chase", "deadlineHours": 24}],
            "drafts": recovery_drafts.build_recovery_drafts(CASE_ROW, "2026-10-27")}
    body = recovery_drafts.build_pack_email_body(pack)
    assert body.index("1. Call Chase (within 24 hours)") < body.index("2. Report to FTC (within 30 days)") < body.index("Call script")
    assert "Pidge sends nothing to anyone else" in body


@pytest.fixture
def dispatch(aws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RECEIVER_USER_ID", RECEIVER_USER_ID)
    monkeypatch.setenv("CASE_AGENT_RUNTIME", "lambda")
    monkeypatch.setenv("CASE_AGENT_MODEL", "haiku-4.5")
    handler = importlib.import_module("pidge_dispatch.handler")
    monkeypatch.setattr(handler, "RECEIVER_MAILBOX", RECEIVER_MAILBOX)
    monkeypatch.setattr(handler, "RECEIPT_SENDER", PIDGE_ADDRESS)
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    monkeypatch.setattr(handler, "_ses_client", ses_client)
    return handler, ses_client


def stored_note(receiver_user_id: str = RECEIVER_USER_ID) -> dict:
    pack = {"caseId": "case-1", "steps": [], "ruleGap": True, "drafts": recovery_drafts.build_recovery_drafts(CASE_ROW, "2026-10-27")}
    return graph.write_case_note("req-1", receiver_user_id, "You reported a payment.", pack, "case:case-1")


def test_the_pack_is_mailed_to_her_own_mailbox_and_the_note_is_stamped(dispatch):
    handler, ses_client = dispatch
    note_row = stored_note()
    with Stubber(ses_client) as stubber:
        stubber.add_response("send_email", {"MessageId": "m1"}, {
            "FromEmailAddress": PIDGE_ADDRESS, "Destination": {"ToAddresses": [RECEIVER_MAILBOX]}, "Content": ANY,
        })
        mailed_row = handler.mail_recovery_pack(note_row)
    assert mailed_row["packMailedAt"].endswith("Z")
    assert graph.get_table().get_item(Key={"PK": note_row["PK"], "SK": note_row["SK"]})["Item"]["packMailedAt"] == mailed_row["packMailedAt"]


def test_a_refused_pack_email_leaves_the_note_unstamped(dispatch, caplog):
    handler, ses_client = dispatch
    note_row = stored_note()
    with Stubber(ses_client) as stubber:
        stubber.add_client_error("send_email", "MessageRejected")
        unmailed_row = handler.mail_recovery_pack(note_row)
    assert "packMailedAt" not in unmailed_row
    assert "packMailedAt" not in graph.get_table().get_item(Key={"PK": note_row["PK"], "SK": note_row["SK"]})["Item"]
    assert "recovery pack email" in caplog.text
