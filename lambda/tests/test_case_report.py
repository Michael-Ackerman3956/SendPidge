"""Send to my bank: the To address comes from config, the body holds case + rule facts only, the receiver only, once."""

from decimal import Decimal

import boto3
from botocore.stub import ANY, Stubber

from shared import cases, graph, verdict
from shared.enums import ActionOutcome, Jurisdiction, PaymentRail, VerdictAnswer
from tests.test_refund_rules import seed_rules
from tests.test_verdict import JIMIN, RECEIVER_ADDRESS, RECEIVER_USER_ID, aws, at, new_request  # noqa: F401

MOCK_BANK = "bank-sim@founder.example"
CASES_ADDRESS = "cases@sendpidge.example"
SENDER = "pidge@sendpidge.example"
SECRET_BODY = "Send 500,000 won to Kakao 3333-01-1234567"


def open_case() -> str:
    seed_rules()
    request_id = new_request()
    graph.get_table().update_item(Key={"PK": "EMAIL#inbox/fake.eml", "SK": "META"}, UpdateExpression="SET textBody = :b",
                                  ExpressionAttributeValues={":b": SECRET_BODY})
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    payment = verdict.PaymentReport(request_id, Jurisdiction.US, PaymentRail.CARD, "Chase", Decimal("500"), "USD", "2026-10-24T01:00:00Z", "ends 4321")
    return verdict.report_payment(payment, RECEIVER_USER_ID, RECEIVER_USER_ID, RECEIVER_ADDRESS).email_row["PK"].removeprefix("CASE#")


def stubbed_ses(expected_to: str, expected_reply_to: str):
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    stubber = Stubber(ses_client)
    stubber.add_response("send_email", {"MessageId": "x"}, {
        "FromEmailAddress": SENDER, "Destination": {"ToAddresses": [expected_to]}, "ReplyToAddresses": [expected_reply_to], "Content": ANY,
    })
    stubber.activate()
    return ses_client, stubber


def test_report_goes_to_the_configured_mailbox_with_case_and_rule_facts_only(aws):
    case_id = open_case()
    ses_client, stubber = stubbed_ses(MOCK_BANK, CASES_ADDRESS)
    result = cases.send_report(case_id, RECEIVER_USER_ID, RECEIVER_USER_ID, "Grandma", ses_client, SENDER, CASES_ADDRESS, MOCK_BANK)
    stubber.assert_no_pending_responses()
    case_row = result.case_row
    assert (result.outcome, case_row["status"], case_row["reportedTo"], case_row["replyCount"]) == (ActionOutcome.RECORDED, "reported", MOCK_BANK, 0)
    assert graph.find_case_id_for_token(case_row["replyToken"]) == case_id
    body = case_row["reportDraft"]
    assert "SIMULATED" in body and "Chase" in body and "number on the back of your card" in body
    assert "3333-01" not in body and "Kakao" not in body and "Scammer" not in body


def test_a_contact_cannot_send_and_a_second_send_is_not_applied(aws):
    case_id = open_case()
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    with Stubber(ses_client):  # any send would raise
        assert cases.send_report(case_id, "jimin", RECEIVER_USER_ID, "Grandma", ses_client, SENDER, CASES_ADDRESS, MOCK_BANK).outcome is ActionOutcome.NOT_ALLOWED
    sent_client, stubber = stubbed_ses(MOCK_BANK, CASES_ADDRESS)
    cases.send_report(case_id, RECEIVER_USER_ID, RECEIVER_USER_ID, "Grandma", sent_client, SENDER, CASES_ADDRESS, MOCK_BANK)
    stubber.assert_no_pending_responses()
    with Stubber(sent_client):
        assert cases.send_report(case_id, RECEIVER_USER_ID, RECEIVER_USER_ID, "Grandma", sent_client, SENDER, CASES_ADDRESS, MOCK_BANK).outcome is ActionOutcome.NOT_APPLIED


def test_close_needs_a_reported_case_and_the_receiver(aws):
    case_id = open_case()
    assert cases.close_case(case_id, RECEIVER_USER_ID, RECEIVER_USER_ID).outcome is ActionOutcome.NOT_APPLIED  # still open
    ses_client, _ = stubbed_ses(MOCK_BANK, CASES_ADDRESS)
    cases.send_report(case_id, RECEIVER_USER_ID, RECEIVER_USER_ID, "Grandma", ses_client, SENDER, CASES_ADDRESS, MOCK_BANK)
    assert cases.close_case(case_id, "jimin", RECEIVER_USER_ID).outcome is ActionOutcome.NOT_ALLOWED
    closed = cases.close_case(case_id, RECEIVER_USER_ID, RECEIVER_USER_ID)
    assert (closed.outcome, closed.case_row["status"]) == (ActionOutcome.RECORDED, "closed")
    assert closed.case_row["closedAt"]
