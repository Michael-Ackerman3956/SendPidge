"""The three drafts come from the case row and the request's final state only (a timeout never reads as a confirmation), the
model's pack tool never carries them, the pack email goes to her own mailbox only, and a refused send leaves the note unstamped
but still pushed."""

import importlib
import json
from decimal import Decimal

import boto3
import pytest
from botocore.stub import ANY, Stubber

from shared import case_agent, graph, recovery_drafts
from shared.enums import CaseTrigger, RequestState
from tests.test_case_agent import denied_request_with_body, report
from tests.test_refund_rules import seed_rules
from tests.test_verdict import RECEIVER_USER_ID, aws  # noqa: F401

CASE_ROW = {"PK": "CASE#case-1", "paymentRail": "zelle", "jurisdiction": "US", "bank": "Chase", "amount": Decimal("500"),
            "currency": "USD", "paidAt": "2026-10-20T09:00:00Z", "payeeHint": "a Kakao account"}
RECEIVER_MAILBOX = "grandma@example.com"
PIDGE_ADDRESS = "pidge@sendpidge.example"


def test_drafts_carry_the_case_facts_and_the_no_answer():
    drafts = recovery_drafts.build_recovery_drafts(CASE_ROW, "2026-10-27", RequestState.DENIED)
    assert set(drafts) == {"callScript", "disputeLetter", "policeReport"}
    for draft in drafts.values():
        assert "Chase" in draft and "500.00 USD" in draft and " to a Kakao account" in draft and "2026-10-20" in draft and "Zelle transfer" in draft
        assert "USD  to" not in draft and "confirmed they never sent it" in draft and "did not answer" not in draft
    assert "I sent 500.00 USD to a Kakao account." in drafts["callScript"]
    assert "Date: 2026-10-27" in drafts["disputeLetter"]
    assert "Call Chase" in drafts["callScript"]
    assert drafts["policeReport"].startswith("What happened, in order:")


def test_a_timeout_case_draft_never_claims_a_confirmation():
    drafts = recovery_drafts.build_recovery_drafts(CASE_ROW, "2026-10-27", RequestState.TIMEOUT)
    for draft in drafts.values():
        assert "confirmed" not in draft and "did not answer, so I could not confirm it with them" in draft
    with pytest.raises(KeyError):  # a payment is reported only after a No or a silence; any other state is a bug, not a sentence
        recovery_drafts.build_recovery_drafts(CASE_ROW, "2026-10-27", RequestState.CONFIRMED)


def test_an_empty_payee_hint_leaves_no_dangling_clause():
    drafts = recovery_drafts.build_recovery_drafts({**CASE_ROW, "payeeHint": ""}, "2026-10-27", RequestState.DENIED)
    assert "500.00 USD through Chase." in drafts["policeReport"] and "500.00 USD." in drafts["callScript"]


def test_pack_email_lists_the_steps_in_order_then_the_drafts():
    pack = {"steps": [{"order": 2, "action": "Report to", "contact": "FTC", "deadlineDays": 30},
                      {"order": 1, "action": "Call", "contact": "Chase", "deadlineHours": 24}],
            "drafts": recovery_drafts.build_recovery_drafts(CASE_ROW, "2026-10-27", RequestState.DENIED)}
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
    pack = {"caseId": "case-1", "steps": [], "ruleGap": True,
            "drafts": recovery_drafts.build_recovery_drafts(CASE_ROW, "2026-10-27", RequestState.DENIED)}
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


def test_a_refused_pack_email_leaves_the_note_unstamped_and_counts_the_failure(dispatch, caplog, monkeypatch):
    handler, ses_client = dispatch
    counted = []
    monkeypatch.setattr(handler, "emit_count_metric", lambda metric_name, *_: counted.append(metric_name))
    note_row = stored_note()
    with Stubber(ses_client) as stubber:
        stubber.add_client_error("send_email", "MessageRejected")
        unmailed_row = handler.mail_recovery_pack(note_row)
    assert "packMailedAt" not in unmailed_row
    assert "packMailedAt" not in graph.get_table().get_item(Key={"PK": note_row["PK"], "SK": note_row["SK"]})["Item"]
    assert "recovery pack email" in caplog.text and counted == ["PackMailFailed"]


def test_no_receiver_mailbox_leaves_the_note_unstamped_and_says_so(dispatch, caplog, monkeypatch):
    handler, _ses_client = dispatch
    monkeypatch.setattr(handler, "RECEIVER_MAILBOX", "")
    note_row = stored_note()
    with caplog.at_level("WARNING"):
        unmailed_row = handler.mail_recovery_pack(note_row)
    assert "packMailedAt" not in unmailed_row and "RECEIVER_MAILBOX is not set" in caplog.text


def test_write_and_push_note_keeps_the_drafts_from_the_model_mails_the_pack_and_pushes_the_stamped_note(dispatch, monkeypatch):
    handler, ses_client = dispatch
    seed_rules()
    request_id = denied_request_with_body()
    case_row = report(request_id)
    scope = case_agent.CaseScope(RECEIVER_USER_ID, request_id, CaseTrigger.CASE, case_row["PK"].removeprefix("CASE#"))
    assert "drafts" not in case_agent.recovery_pack_for_scope(scope)  # what the agent's tool returns
    monkeypatch.setattr(handler.case_agent, "build_case_agent", lambda *_: object())
    monkeypatch.setattr(handler.case_agent, "run_case_agent",
                        lambda _agent, _scope: case_agent.CaseNote(note_text="You reported a payment.", cites_previous_case=False))
    pushed = []
    monkeypatch.setattr(handler.channels, "push_to_user", lambda _user_id, message: pushed.append(json.loads(handler.channels.encode_push_message(message))) or 1)
    with Stubber(ses_client) as stubber:
        stubber.add_response("send_email", {"MessageId": "m1"}, {"FromEmailAddress": ANY, "Destination": ANY, "Content": ANY})
        handler.write_and_push_note(scope)
    [note_message] = pushed
    note = note_message["note"]
    assert note["packMailedAt"].endswith("Z")
    assert "confirmed they never sent it" in note["recoveryPack"]["drafts"]["disputeLetter"]
    assert "ends 4321" in note["recoveryPack"]["drafts"]["callScript"]  # the payee hint reaches her drafts, never the model
