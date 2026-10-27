"""The Reader's resident is the envelope recipient's RECEIVER#<address>/PROFILE: an address with no profile, or an event with
other than one recipient, is dropped and counted with nothing written, and the To header never picks the resident."""

import importlib
import json

import boto3
import pytest
from boto3.dynamodb.conditions import Attr

from shared import graph
from shared.profiles import UNKNOWN_RECEIVER_METRIC_NAME
from shared.reader_agent import EmailVerdict
from tests.helpers.profiles import seed_resident
from tests.helpers.scripted_model import ScriptedModel, verdict_step
from tests.test_reader_pipeline import FAMILY_HELLO_NO_ASK
from tests.test_reader_reading_state import inbound_event
from tests.test_verdict import aws  # noqa: F401

BUCKET_NAME = "bucket"
GRANDMA_ADDRESS = "grandma@example.com"
BOB_ADDRESS = "bob@example.com"
THANK_YOU_SUBJECT = "Photos from the weekend"


def raw_email_to_grandma(message_id: str) -> bytes:
    return (f"From: Jimin <jimin@example.com>\r\nTo: Grandma <{GRANDMA_ADDRESS}>\r\nSubject: {THANK_YOU_SUBJECT}\r\n"
            f"Message-ID: <{message_id}@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            "Thank you for the lovely weekend.\r\n").encode()


def store_inbox_email(message_id: str) -> None:
    boto3.client("s3").put_object(Bucket=BUCKET_NAME, Key=f"inbox/{message_id}", Body=raw_email_to_grandma(message_id))


def count_unknown_receiver_metric_lines(stdout: str) -> int:
    return sum(1 for line in stdout.splitlines() if line.startswith("{") and json.loads(line).get(UNKNOWN_RECEIVER_METRIC_NAME) == 1)


def rows_with_partition_prefix(prefix: str) -> list[dict]:
    return graph.get_table().scan(FilterExpression=Attr("PK").begins_with(prefix))["Items"]


@pytest.fixture
def reader_with_bucket(aws, monkeypatch):  # noqa: F811
    """pidge_reader.handler on moto's table and bucket; every push recorded with the user it went to."""
    for name, value in {"EMAIL_BUCKET": BUCKET_NAME, "READER_MODEL": "haiku-4.5", "ESCALATION_MODEL": "opus-4.5",
                        "ESCALATION_ENABLED": "false", "RETRY_SCHEDULER_ENABLED": "false"}.items():
        monkeypatch.setenv(name, value)
    handler = importlib.import_module("pidge_reader.handler")
    importlib.reload(handler)
    boto3.client("s3").create_bucket(Bucket=BUCKET_NAME)
    pushes: list[tuple[str, dict]] = []
    monkeypatch.setattr(handler.channels, "push_to_user", lambda user_id, message: pushes.append((user_id, message)) or 1)
    return handler, pushes


def test_unknown_receiver_dropped_never_defaulted(reader_with_bucket, monkeypatch, capsys):
    handler, pushes = reader_with_bucket
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma")
    store_inbox_email("msg-unknown-0001")  # addressed To Grandma: the header must not rescue it
    scripted_reader_model = ScriptedModel(EmailVerdict, {})
    handler.reader.model = scripted_reader_model
    fetched_keys = []
    production_get_object = handler.s3_client.get_object
    def record_fetch(Bucket, Key):
        fetched_keys.append(Key)
        return production_get_object(Bucket=Bucket, Key=Key)
    monkeypatch.setattr(handler.s3_client, "get_object", record_fetch)
    capsys.readouterr()
    for recipients in (["stranger@example.com"], [], [GRANDMA_ADDRESS, "stranger@example.com"]):
        assert handler.handle(inbound_event("msg-unknown-0001", recipients), None) is None
        assert count_unknown_receiver_metric_lines(capsys.readouterr().out) == 1, recipients
    assert rows_with_partition_prefix("EMAIL#") == [] and rows_with_partition_prefix("REQUEST#") == []
    assert pushes == [] and fetched_keys == []
    assert scripted_reader_model.recorded_calls == []


def test_to_header_ignored_when_envelope_recipient_differs(reader_with_bucket):
    handler, pushes = reader_with_bucket
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma")
    seed_resident(BOB_ADDRESS, "bob", "Bob")
    store_inbox_email("msg-bob-0001")  # To Grandma, delivered to Bob
    handler.reader.model = ScriptedModel(EmailVerdict, {THANK_YOU_SUBJECT: [verdict_step(FAMILY_HELLO_NO_ASK)]})
    handler.handle(inbound_event("msg-bob-0001", ["Bob@Example.com"]), None)
    email_row = graph.get_email("inbox/msg-bob-0001")
    assert (email_row["receiverEmail"], email_row[graph.USER_INDEX_PARTITION_KEY], email_row["s3Key"]) == (
        BOB_ADDRESS, "bob#EMAIL", "inbox/msg-bob-0001")
    assert [user_id for user_id, _message in pushes] == ["bob", "bob"]  # reading, then the verdict
