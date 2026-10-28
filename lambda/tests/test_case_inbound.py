"""A bank reply: only a tokened reply from the reported address with passing auth lands, once per object, on an open case;
the request's contact is nudged unless it is a home's office."""

import importlib
from decimal import Decimal

import boto3
from botocore.stub import ANY, Stubber

from shared import case_agent, cases, graph, verdict
from shared.enums import ActionOutcome, CaseTrigger, Channel, Jurisdiction, PaymentRail, ReasonCode, VerdictAnswer
from tests.test_case_report import CASES_ADDRESS, MOCK_BANK, SENDER, grandma, open_case, stubbed_ses
from tests.test_org import SUNRISE_ORG_ID, add_office_to_circle, new_office_request, seed_sunrise_and_maple
from tests.test_refund_rules import seed_rules
from tests.test_verdict import RECEIVER_ADDRESS, RECEIVER_USER_ID, at, aws  # noqa: F401

REPLY_TEXT = "We opened dispute 4471 and blocked the card. A new card ships tomorrow."


def reported_case() -> dict:
    case_id = open_case()
    ses_client, _ = stubbed_ses(MOCK_BANK, CASES_ADDRESS)
    return cases.send_report(case_id, RECEIVER_USER_ID, grandma(), ses_client, SENDER, CASES_ADDRESS, MOCK_BANK).case_row


def reply(case_row: dict, s3_key: str = "cases/reply-1.eml", sender: str = MOCK_BANK, subject: str | None = None, auth=()) -> cases.InboundCaseReply:
    return cases.InboundCaseReply(s3_key, sender, subject if subject is not None else f"Re: {cases.report_subject(case_row['replyToken'])}", REPLY_TEXT, tuple(auth))


def test_a_tokened_reply_from_the_reported_address_lands_once(aws):
    case_row = reported_case()
    case_id = case_row["PK"].removeprefix("CASE#")
    result = cases.record_bank_reply(reply(case_row))
    assert (result.outcome, result.case_row["replyCount"]) == (ActionOutcome.RECORDED, 1)
    assert result.case_row["bankRepliedAt"]
    assert cases.record_bank_reply(reply(case_row)).outcome is ActionOutcome.NOT_APPLIED  # same S3 key twice
    replies = graph.list_case_replies(case_id)
    assert len(replies) == 1 and replies[0]["textContent"] == REPLY_TEXT and replies[0]["from"] == MOCK_BANK
    assert cases.record_bank_reply(reply(case_row, s3_key="cases/reply-2.eml")).case_row["replyCount"] == 2


def test_unknown_token_other_sender_and_failed_auth_are_dropped(aws):
    case_row = reported_case()
    case_id = case_row["PK"].removeprefix("CASE#")
    assert cases.record_bank_reply(reply(case_row, subject="Re: Pidge case [not-a-real-token]")).outcome is ActionOutcome.UNKNOWN_REQUEST
    assert cases.record_bank_reply(reply(case_row, subject="Re: hello")).outcome is ActionOutcome.UNKNOWN_REQUEST
    assert cases.record_bank_reply(reply(case_row, sender="scammer@evil.example")).outcome is ActionOutcome.WRONG_SENDER
    assert cases.record_bank_reply(reply(case_row, auth=(ReasonCode.AUTH_FAILED,))).outcome is ActionOutcome.WRONG_SENDER
    assert graph.list_case_replies(case_id) == []
    assert "bankRepliedAt" not in graph.get_case(case_id)


def test_a_reply_on_a_closed_case_is_not_applied(aws):
    case_row = reported_case()
    case_id = case_row["PK"].removeprefix("CASE#")
    cases.close_case(case_id, RECEIVER_USER_ID, grandma())
    assert cases.record_bank_reply(reply(case_row)).outcome is ActionOutcome.NOT_APPLIED
    assert graph.list_case_replies(case_id) == []


def test_bank_reply_nudge_never_reaches_the_org(aws, monkeypatch):
    seed_sunrise_and_maple()
    seed_rules()
    office = add_office_to_circle(RECEIVER_ADDRESS, SUNRISE_ORG_ID)
    request_id = new_office_request("inbox/office-1.eml", office)
    assert verdict.record_answer(request_id, VerdictAnswer.NO, verdict.SenderIdentity(Channel.INAPP, "ann"), "m1", at(0)).outcome is ActionOutcome.RECORDED
    payment = verdict.PaymentReport(request_id, Jurisdiction.US, PaymentRail.CARD, "Chase", Decimal("500"), "USD", "2026-10-24T01:00:00Z", "ends 4321")
    case_id = graph.case_id_of(verdict.report_payment(payment, RECEIVER_USER_ID, RECEIVER_USER_ID, RECEIVER_ADDRESS).email_row)
    ses_client, _ = stubbed_ses(MOCK_BANK, CASES_ADDRESS)
    reported_row = cases.send_report(case_id, RECEIVER_USER_ID, grandma(), ses_client, SENDER, CASES_ADDRESS, MOCK_BANK).case_row
    replied = cases.record_bank_reply(reply(reported_row))
    assert replied.outcome is ActionOutcome.RECORDED
    handler = importlib.import_module("pidge_case_inbound.handler")
    pushes = []
    monkeypatch.setattr(handler.channels, "push_to_user", lambda user_id, message: pushes.append((user_id, message["type"])) or 1)
    handler.push_bank_reply(case_id, replied.case_row, grandma())
    assert pushes == [(RECEIVER_USER_ID, "case_state")]  # the resident's case card only: no nudge to org-sunrise's admins


def test_get_case_never_returns_reply_rows(aws):
    case_row = reported_case()
    case_id = case_row["PK"].removeprefix("CASE#")
    cases.record_bank_reply(reply(case_row))
    scope = case_agent.CaseScope(RECEIVER_USER_ID, case_row["requestId"], CaseTrigger.CASE_CLOSED, case_id)
    metadata = case_agent.case_metadata(scope)
    assert metadata["reportedPayment"]["timeline"] == {"closedAt": None}  # no bank loop: the agent never hears of a reply
    assert "bankReplyCount" not in metadata["reportedPayment"]
    assert "4471" not in str(metadata) and REPLY_TEXT not in str(metadata)
    assert "replyToken" not in str(metadata) and case_row["replyToken"] not in str(metadata)
