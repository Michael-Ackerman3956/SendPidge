"""The verdict rules, run against DynamoDB's own condition evaluator (moto), not a copy of them."""

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from shared import graph, inbox_rows, lists, payloads, verdict
from shared.enums import ActionOutcome, Channel, Friction, ListSource, RequestState, VerdictAnswer

RECEIVER_ADDRESS = "grandma@example.com"
RECEIVER_USER_ID = "grandma"
CONTACT_EMAIL = "jimin@example.com"
BUCKET_NAME = "test-emails"
EMAIL_ID = "inbox/fake.eml"
JIMIN = verdict.SenderIdentity(Channel.INAPP, "jimin")
VERDICT_FIELDS = {"claimed_person": "Jimin", "intent": "money", "confidence": 0.95, "summary": "Asks for money."}


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "sendpidge-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="sendpidge-test",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"},
                                  {"AttributeName": graph.USER_INDEX_PARTITION_KEY, "AttributeType": "S"},
                                  {"AttributeName": graph.USER_INDEX_SORT_KEY, "AttributeType": "S"}],
            GlobalSecondaryIndexes=[{  # the same UserIndex the CDK table declares
                "IndexName": graph.USER_INDEX_NAME,
                "KeySchema": [{"AttributeName": graph.USER_INDEX_PARTITION_KEY, "KeyType": "HASH"},
                              {"AttributeName": graph.USER_INDEX_SORT_KEY, "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        graph._table = None  # the module caches the table per container
        yield
        graph._table = None


JIMIN_RCS_NUMBER = "+821012345678"
JIMIN_WHATSAPP_NUMBER = "+821012345678"


def seed_contact(contact_email: str | None = CONTACT_EMAIL) -> graph.Contact:
    row = {"PK": f"RECEIVER#{RECEIVER_ADDRESS}", "SK": "CONTACT#jimin", "contactName": "Jimin",
           "contactPwaUserId": "jimin", "contactRcs": JIMIN_RCS_NUMBER, "contactWhatsApp": JIMIN_WHATSAPP_NUMBER}
    if contact_email:
        row["contactEmail"] = contact_email
    graph.get_table().put_item(Item=row)
    return graph.Contact("CONTACT#jimin", "Jimin", "jimin")


def new_request() -> str:
    inbox_rows.claim_row(Channel.EMAIL, EMAIL_ID, {"s3Key": EMAIL_ID, "senderAddress": "Scammer@Lookalike.example", "quarantined": False})
    return graph.request_id_of(graph.create_request(EMAIL_ID, RECEIVER_ADDRESS, seed_contact(), VERDICT_FIELDS, Friction.SINGLE_TAP, None,
                                                    Channel.EMAIL))


BOB_ADDRESS = "bob@example.com"
BOB_USER_ID = "bob"
BOB_EMAIL_ID = "inbox/bob.eml"


def new_request_to_bob_from_the_same_sender() -> str:
    """The same scammer writing to a second resident, Bob, who also has Jimin as a contact."""
    inbox_rows.claim_row(Channel.EMAIL, BOB_EMAIL_ID, {"s3Key": BOB_EMAIL_ID, "senderAddress": "Scammer@Lookalike.example", "quarantined": False})
    graph.get_table().put_item(Item={"PK": f"RECEIVER#{BOB_ADDRESS}", "SK": "CONTACT#jimin", "contactName": "Jimin",
                                     "contactPwaUserId": "jimin"})
    bobs_contact = graph.Contact("CONTACT#jimin", "Jimin", "jimin")
    return graph.request_id_of(graph.create_request(BOB_EMAIL_ID, BOB_ADDRESS, bobs_contact, VERDICT_FIELDS, Friction.SINGLE_TAP, None,
                                                    Channel.EMAIL))


def confirm_as_grandma(request_id: str, s3_client) -> None:
    confirmed = verdict.confirm_quarantine(request_id, RECEIVER_USER_ID, RECEIVER_USER_ID, RECEIVER_ADDRESS, s3_client, BUCKET_NAME)
    assert confirmed.outcome is ActionOutcome.RECORDED


def read_listed_sender() -> dict | None:
    """LIST#address/<the scammer's address>, or None when there is no row."""
    response = graph.get_table().get_item(Key={"PK": "LIST#address", "SK": "scammer@lookalike.example"})
    return response["Item"] if "Item" in response else None


def at(seconds_from_now: int) -> str:
    return graph.iso_timestamp(datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now))


def status_of(request_id: str) -> str:
    return graph.get_request(request_id)["status"]


def test_timestamps_are_fixed_width_utc():
    assert graph.iso_timestamp(datetime(2026, 10, 22, 9, 5, 7, 123456, tzinfo=timezone.utc)) == "2026-10-22T09:05:07Z"
    korea = timezone(timedelta(hours=9))
    assert graph.iso_timestamp(datetime(2026, 10, 22, 18, 5, 7, tzinfo=korea)) == "2026-10-22T09:05:07Z"


def test_no_while_pending_denies(aws):
    request_id = new_request()
    result = verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    assert (result.outcome, result.status, status_of(request_id)) == (ActionOutcome.RECORDED, RequestState.DENIED, "denied")


def test_yes_inside_the_hold_waits_for_release(aws):
    request_id = new_request()
    result = verdict.record_answer(request_id, VerdictAnswer.YES, JIMIN, "m1", at(0))
    request_row = graph.get_request(request_id)
    assert (result.outcome, result.status, request_row["status"]) == (ActionOutcome.RECORDED, RequestState.CONFIRMED_PENDING, "confirmed_pending")
    assert request_row["releaseAt"] == verdict.release_time_for(request_row["resolvedAt"])
    assert verdict.effective_status(request_row, at(0)) is RequestState.CONFIRMED_PENDING
    assert verdict.effective_status(request_row, at(verdict.RELEASE_DELAY_SECONDS + 1)) is RequestState.CONFIRMED


def test_undo_within_the_release_delay_denies(aws):
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.YES, JIMIN, "m1", at(0))
    result = verdict.record_answer(request_id, VerdictAnswer.UNDO, JIMIN, "m2", at(5))
    assert (result.outcome, result.status, status_of(request_id)) == (ActionOutcome.RECORDED, RequestState.DENIED, "denied")


def test_undo_after_the_release_delay_is_ignored(aws):
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.YES, JIMIN, "m1", at(0))
    result = verdict.record_answer(request_id, VerdictAnswer.UNDO, JIMIN, "m2", at(verdict.RELEASE_DELAY_SECONDS + 1))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.NOT_APPLIED, "confirmed_pending")


def test_release_confirms_only_while_still_pending(aws):
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.YES, JIMIN, "m1", at(0))
    released = verdict.release_if_still_pending(request_id)
    assert (released.outcome, released.status, status_of(request_id)) == (ActionOutcome.RECORDED, RequestState.CONFIRMED, "confirmed")
    assert verdict.release_if_still_pending(request_id).outcome is ActionOutcome.NOT_APPLIED
    undone_id = new_request()
    verdict.record_answer(undone_id, VerdictAnswer.YES, JIMIN, "m1", at(0))
    verdict.record_answer(undone_id, VerdictAnswer.UNDO, JIMIN, "m2", at(1))
    assert (verdict.release_if_still_pending(undone_id).outcome, status_of(undone_id)) == (ActionOutcome.NOT_APPLIED, "denied")


def test_reverse_within_five_minutes_takes_the_no_back_and_drops_the_row_its_own_confirm_wrote(aws):
    s3_client = make_bucket_with_email()
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    confirm_as_grandma(request_id, s3_client)
    assert read_listed_sender()["requestId"] == request_id
    result = verdict.record_answer(request_id, VerdictAnswer.REVERSE, JIMIN, "m2", at(verdict.REVERSE_WINDOW_SECONDS - 1))
    assert (result.outcome, result.status, status_of(request_id)) == (ActionOutcome.RECORDED, RequestState.REVERSED_NO, "reversed_no")
    assert read_listed_sender() is None


def test_reverse_leaves_the_row_another_residents_confirm_wrote_for_the_same_sender(aws):
    s3_client = make_bucket_with_email()
    s3_client.put_object(Bucket=BUCKET_NAME, Key=BOB_EMAIL_ID, Body=b"raw")
    grandmas_request_id = new_request()
    verdict.record_answer(grandmas_request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    confirm_as_grandma(grandmas_request_id, s3_client)
    bobs_request_id = new_request_to_bob_from_the_same_sender()
    verdict.record_answer(bobs_request_id, VerdictAnswer.NO, JIMIN, "m2", at(0))
    bobs_confirm = verdict.confirm_quarantine(bobs_request_id, BOB_USER_ID, BOB_USER_ID, BOB_ADDRESS, s3_client, BUCKET_NAME)
    assert bobs_confirm.outcome is ActionOutcome.RECORDED

    reversed_answer = verdict.record_answer(grandmas_request_id, VerdictAnswer.REVERSE, JIMIN, "m3", at(1))

    assert reversed_answer.outcome is ActionOutcome.RECORDED
    listed_row = read_listed_sender()
    assert (listed_row["source"], listed_row["requestId"]) == (ListSource.PIDGE_CONFIRMED, bobs_request_id)


# The feed-sourced address row is hand-built here, not synced through feed_parsers (AMF writes address rows that way).
@pytest.mark.parametrize("listed_row", [
    lists.list_row_from_seed({"kind": "address", "value": "scammer@lookalike.example", "category": "scam", "strength": "listed",
                              "reason": "Sample impersonation address", "addedAt": "2026-10-25T00:00:00Z"}),
    {"PK": "LIST#address", "SK": "scammer@lookalike.example", "category": "scam", "strength": "listed",
     "source": str(ListSource.PUBLIC_FEED), "sourceId": "feed-test", "tier": 1, "reason": "Listed by a feed",
     "attribution": "feed-test", "addedAt": "2026-10-25T00:00:00Z", "expiresAt": None, "generation": 1},
], ids=["sample", "public-feed"])
def test_reverse_leaves_a_sample_or_feed_row_for_the_sender(aws, listed_row):
    graph.get_table().put_item(Item=listed_row)
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    result = verdict.record_answer(request_id, VerdictAnswer.REVERSE, JIMIN, "m2", at(1))
    assert result.outcome is ActionOutcome.RECORDED
    assert read_listed_sender() == listed_row


def test_reverse_after_five_minutes_or_without_a_no_is_ignored(aws):
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    late = verdict.record_answer(request_id, VerdictAnswer.REVERSE, JIMIN, "m2", at(verdict.REVERSE_WINDOW_SECONDS + 1))
    assert (late.outcome, status_of(request_id)) == (ActionOutcome.NOT_APPLIED, "denied")
    pending_id = new_request()
    assert verdict.record_answer(pending_id, VerdictAnswer.REVERSE, JIMIN, "m1", at(0)).outcome is ActionOutcome.NOT_APPLIED


def test_late_yes_is_ignored(aws):
    request_id = new_request()
    result = verdict.record_answer(request_id, VerdictAnswer.YES, JIMIN, "m1", at(graph.HOLD_DURATION_SECONDS + 1))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.NOT_APPLIED, "pending")


def test_no_after_a_reversal_lands_again(aws):
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    verdict.record_answer(request_id, VerdictAnswer.REVERSE, JIMIN, "m2", at(1))
    result = verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m3", at(2))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.RECORDED, "denied")


def test_late_no_still_lands_and_overrides_an_earlier_yes(aws):
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.YES, JIMIN, "m1", at(0))
    result = verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m2", at(600))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.RECORDED, "denied")


def test_yes_after_no_is_ignored(aws):
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    result = verdict.record_answer(request_id, VerdictAnswer.YES, JIMIN, "m2", at(1))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.NOT_APPLIED, "denied")


def test_redelivered_message_id_is_ignored(aws):
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.YES, JIMIN, "m1", at(0))
    result = verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(1))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.NOT_APPLIED, "confirmed_pending")


def test_answer_from_another_user_is_rejected(aws):
    request_id = new_request()
    stranger = verdict.SenderIdentity(Channel.INAPP, "grandma")
    result = verdict.record_answer(request_id, VerdictAnswer.YES, stranger, "m1", at(0))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.WRONG_SENDER, "pending")


def test_whatsapp_no_from_the_registered_number_lands(aws):
    request_id = new_request()
    whatsapp_sender = verdict.SenderIdentity(Channel.WHATSAPP, JIMIN_WHATSAPP_NUMBER)
    result = verdict.record_answer(request_id, VerdictAnswer.NO, whatsapp_sender, "whatsapp:wamid.1", at(0))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.RECORDED, "denied")


def test_whatsapp_from_a_number_without_the_plus_is_rejected(aws):
    request_id = new_request()
    digits_only = verdict.SenderIdentity(Channel.WHATSAPP, JIMIN_WHATSAPP_NUMBER.removeprefix("+"))
    result = verdict.record_answer(request_id, VerdictAnswer.NO, digits_only, "whatsapp:wamid.1", at(0))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.WRONG_SENDER, "pending")


def test_rcs_no_from_the_registered_number_lands(aws):
    request_id = new_request()
    rcs_sender = verdict.SenderIdentity(Channel.RCS, JIMIN_RCS_NUMBER)
    result = verdict.record_answer(request_id, VerdictAnswer.NO, rcs_sender, "rcs:m1", at(0))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.RECORDED, "denied")


def test_rcs_from_another_number_is_rejected(aws):
    request_id = new_request()
    stranger = verdict.SenderIdentity(Channel.RCS, "+821099999999")
    result = verdict.record_answer(request_id, VerdictAnswer.NO, stranger, "rcs:m1", at(0))
    assert (result.outcome, status_of(request_id)) == (ActionOutcome.WRONG_SENDER, "pending")


def test_redelivered_rcs_tap_is_ignored(aws):
    request_id = new_request()
    rcs_sender = verdict.SenderIdentity(Channel.RCS, JIMIN_RCS_NUMBER)
    verdict.record_answer(request_id, VerdictAnswer.NO, rcs_sender, "rcs:m1", at(0))
    result = verdict.record_answer(request_id, VerdictAnswer.NO, rcs_sender, "rcs:m1", at(1))
    assert result.outcome is ActionOutcome.NOT_APPLIED


def test_unknown_request_is_rejected(aws):
    seed_contact()
    result = verdict.record_answer("req-missing", VerdictAnswer.NO, JIMIN, "m1", at(0))
    assert result.outcome is ActionOutcome.UNKNOWN_REQUEST


def test_receipt_goes_only_to_the_contact_email():
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    with Stubber(ses_client) as stubber:
        stubber.add_response("send_email", {"MessageId": "x"}, {
            "FromEmailAddress": "pidge@example.com",
            "Destination": {"ToAddresses": [CONTACT_EMAIL]},
            "Content": {"Simple": {
                "Subject": {"Data": "Pidge: your answer was recorded"},
                "Body": {"Text": {"Data": "You told Pidge you did not send the email to Grandma. It stays held."}},
            }},
        })
        contact_row = {"SK": "CONTACT#jimin", "contactEmail": CONTACT_EMAIL}
        assert verdict.send_receipt(ses_client, "pidge@example.com", contact_row, RequestState.DENIED, "Grandma")
        stubber.assert_no_pending_responses()


def test_no_receipt_while_a_yes_is_still_waiting_to_release():
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    with Stubber(ses_client):  # any call would raise: no responses queued
        contact_row = {"SK": "CONTACT#jimin", "contactEmail": CONTACT_EMAIL}
        assert not verdict.send_receipt(ses_client, "pidge@example.com", contact_row, RequestState.CONFIRMED_PENDING, "Grandma")


def test_no_receipt_without_a_contact_email():
    ses_client = boto3.client("sesv2", region_name="us-east-1")
    with Stubber(ses_client):  # any call would raise: no responses queued
        assert not verdict.send_receipt(ses_client, "pidge@example.com", {"SK": "CONTACT#jimin"},
                                        RequestState.DENIED, "Grandma")


def make_bucket_with_email():
    s3_client = boto3.client("s3")
    s3_client.create_bucket(Bucket=BUCKET_NAME)
    s3_client.put_object(Bucket=BUCKET_NAME, Key=EMAIL_ID, Body=b"raw")
    return s3_client


def test_confirm_after_no_quarantines_and_lists_the_full_address(aws):
    s3_client = make_bucket_with_email()
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    result = verdict.confirm_quarantine(request_id, RECEIVER_USER_ID, RECEIVER_USER_ID, RECEIVER_ADDRESS,
                                        s3_client, BUCKET_NAME)
    assert result.outcome is ActionOutcome.RECORDED
    assert s3_client.get_object_tagging(Bucket=BUCKET_NAME, Key=EMAIL_ID)["TagSet"] == [verdict.QUARANTINE_TAG]
    assert inbox_rows.get_row(Channel.EMAIL, EMAIL_ID)["quarantined"] is True
    listed = graph.get_table().get_item(Key={"PK": "LIST#address", "SK": "scammer@lookalike.example"})["Item"]
    assert (listed["source"], listed["tier"]) == ("pidge-confirmed", 0)
    assert "Item" not in graph.get_table().get_item(Key={"PK": "LIST#domain", "SK": "lookalike.example"})


def test_confirm_before_any_no_is_not_applied(aws):
    s3_client = make_bucket_with_email()
    request_id = new_request()
    result = verdict.confirm_quarantine(request_id, RECEIVER_USER_ID, RECEIVER_USER_ID, RECEIVER_ADDRESS,
                                        s3_client, BUCKET_NAME)
    assert result.outcome is ActionOutcome.NOT_APPLIED
    assert inbox_rows.get_row(Channel.EMAIL, EMAIL_ID)["quarantined"] is False


def test_confirm_by_the_contact_is_not_allowed(aws):
    s3_client = make_bucket_with_email()
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    result = verdict.confirm_quarantine(request_id, "jimin", RECEIVER_USER_ID, RECEIVER_ADDRESS,
                                        s3_client, BUCKET_NAME)
    assert result.outcome is ActionOutcome.NOT_ALLOWED
    assert inbox_rows.get_row(Channel.EMAIL, EMAIL_ID)["quarantined"] is False


def test_a_request_row_written_before_slice_8_still_makes_a_one_tap_card():
    request_row = {"PK": "REQUEST#req-old", "verdict": {"summary": "Jimin asks for money"}, "expiresAt": "2026-10-21T23:40:49Z"}
    card = payloads.card_for_contact(request_row, None, "Grandma", RequestState.PENDING)
    assert card["friction"] == Friction.SINGLE_TAP
