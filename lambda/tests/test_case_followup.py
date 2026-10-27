"""The one reminder: a silent case gets exactly one; a replied or closed case gets none."""

import boto3
from botocore.stub import ANY, Stubber

from shared import cases
from shared.enums import ActionOutcome
from tests.test_case_inbound import reply, reported_case
from tests.test_case_report import CASES_ADDRESS, MOCK_BANK, SENDER, grandma
from tests.test_verdict import RECEIVER_USER_ID, aws  # noqa: F401


def reminder_client():
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    stubber = Stubber(ses_client)
    stubber.add_response("send_email", {"MessageId": "r"}, {
        "FromEmailAddress": SENDER, "Destination": {"ToAddresses": [MOCK_BANK]}, "ReplyToAddresses": [CASES_ADDRESS], "Content": ANY,
    })
    stubber.activate()
    return ses_client, stubber


def test_a_silent_case_gets_exactly_one_reminder(aws):
    case_row = reported_case()
    case_id = case_row["PK"].removeprefix("CASE#")
    ses_client, stubber = reminder_client()
    first = cases.send_follow_up(case_id, ses_client, SENDER, CASES_ADDRESS)
    stubber.assert_no_pending_responses()
    assert first.outcome is ActionOutcome.RECORDED and first.case_row["followUpSentAt"]
    with Stubber(ses_client):  # a second send would raise
        assert cases.send_follow_up(case_id, ses_client, SENDER, CASES_ADDRESS).outcome is ActionOutcome.NOT_APPLIED


def test_a_replied_or_closed_case_gets_no_reminder(aws):
    replied = reported_case()
    cases.record_bank_reply(reply(replied))
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    with Stubber(ses_client):
        assert cases.send_follow_up(replied["PK"].removeprefix("CASE#"), ses_client, SENDER, CASES_ADDRESS).outcome is ActionOutcome.NOT_APPLIED
    closed = reported_case()
    cases.close_case(closed["PK"].removeprefix("CASE#"), RECEIVER_USER_ID, grandma())
    with Stubber(ses_client):
        assert cases.send_follow_up(closed["PK"].removeprefix("CASE#"), ses_client, SENDER, CASES_ADDRESS).outcome is ActionOutcome.NOT_APPLIED
