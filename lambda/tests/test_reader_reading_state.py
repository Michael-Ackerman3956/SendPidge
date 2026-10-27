"""The Reader tells Grandma's screen "reading" before the verdict and without the body, and a Reader that crashed after
claiming an email is run again by its retry instead of leaving the row on "reading" for ever."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared import graph
from shared.enums import Channel, Intent
from shared.reader_agent import EmailVerdict
from tests.test_lists import reader_handler  # noqa: F401
from tests.test_verdict import aws  # noqa: F401

SAMPLE_PATH = Path(__file__).resolve().parent.parent.parent / "samples" / "fake-jimin.eml"
OBJECT_KEY = "inbox/fake-jimin.eml"
READER_RECEIVER_ADDRESS = "grandma@sendpidge.example"  # the reader_handler fixture's RECEIVER_ADDRESS


def seed_jimin_for_the_reader() -> None:
    graph.get_table().put_item(Item={"PK": f"RECEIVER#{READER_RECEIVER_ADDRESS}", "SK": "CONTACT#jimin", "contactName": "Jimin",
                                     "contactPwaUserId": "jimin", "knownAddresses": ["jimin@example.com"], "verifiedVia": "seed"})


def run_reader_on_the_sample(handler, monkeypatch) -> None:
    raw_email = SAMPLE_PATH.read_bytes()
    monkeypatch.setattr(handler.s3_client, "get_object", lambda Bucket, Key: {"Body": type("Body", (), {"read": lambda self: raw_email})()})
    monkeypatch.setattr(handler, "read_email", lambda _reader, _email: EmailVerdict(
        claimed_person="Jimin", intent=Intent.MONEY, confidence=0.95, summary="Asks for money for a hospital bill."))
    async def asked_in_app(*_):
        return [Channel.INAPP]
    monkeypatch.setattr(handler.channels, "ask_on_every_channel", asked_in_app)
    handler.process_email_object("bucket", OBJECT_KEY)


def test_the_reading_push_precedes_the_verdict_push_and_carries_no_body(reader_handler, monkeypatch):  # noqa: F811
    handler, pushed = reader_handler
    seed_jimin_for_the_reader()
    run_reader_on_the_sample(handler, monkeypatch)
    assert [email["state"] for email in pushed] == [None, "hold"]
    assert pushed[0]["textBody"] == "" and pushed[0]["links"] == [] and pushed[0]["emailId"] == OBJECT_KEY
    assert pushed[1]["textBody"] != ""


def test_a_retry_runs_an_email_the_crashed_reader_claimed_but_a_fresh_duplicate_is_refused(reader_handler, monkeypatch):  # noqa: F811
    handler, pushed = reader_handler
    seed_jimin_for_the_reader()
    now = datetime.now(timezone.utc)
    assert graph.claim_email(OBJECT_KEY, {"s3Key": OBJECT_KEY, "receivedAt": "2026-10-27T06:00:00Z"})
    run_reader_on_the_sample(handler, monkeypatch)  # the duplicate arrives seconds after the claim: another run holds it
    assert pushed == [] and "state" not in graph.get_email(OBJECT_KEY)
    old_claim = graph.iso_timestamp(now - timedelta(seconds=graph.READER_RECLAIM_AFTER_SECONDS + 1))
    graph.get_table().update_item(Key={"PK": f"EMAIL#{OBJECT_KEY}", "SK": "META"}, UpdateExpression="SET claimedAt = :at",
                                  ExpressionAttributeValues={":at": old_claim})
    run_reader_on_the_sample(handler, monkeypatch)  # the retry a minute later finds the claim abandoned
    assert [email["state"] for email in pushed] == [None, "hold"]
    assert graph.get_email(OBJECT_KEY)["state"] == "hold"
    assert graph.reclaim_abandoned_email(OBJECT_KEY, now) is False  # decided rows are never run again
