"""S3 pidge/ → Grandma's question by email. Only a mail from her real mailbox with aligned DKIM becomes a CHAT# row;
pidge-dispatch answers it from the stream. Everything else is dropped, logged and counted. No Bedrock here."""

import json
import logging
import os
import time
from urllib.parse import unquote_plus

import boto3

from shared import chat_email
from shared.enums import EmailedQuestionOutcome

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIVER_MAILBOX = os.environ["RECEIVER_MAILBOX"]
DROPPED_METRIC_NAME = "EmailedQuestionDropped"

s3_client = boto3.client("s3")


def handle(event, context):
    for record in event["Records"]:
        handle_question_object(record["s3"]["bucket"]["name"], unquote_plus(record["s3"]["object"]["key"]))


def emit_dropped_metric(outcome: EmailedQuestionOutcome) -> None:
    """CloudWatch embedded metric format: a count per drop reason, no extra permissions needed."""
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [
            {"Namespace": "SendPidge", "Dimensions": [["Outcome"]], "Metrics": [{"Name": DROPPED_METRIC_NAME, "Unit": "Count"}]}]},
        "Outcome": str(outcome), DROPPED_METRIC_NAME: 1,
    }))


def handle_question_object(bucket_name: str, object_key: str) -> None:
    raw_email = s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read()
    outcome = chat_email.accept_emailed_question(raw_email, RECEIVER_MAILBOX, RECEIVER_USER_ID)
    if outcome is not EmailedQuestionOutcome.ACCEPTED:
        logger.warning("emailed question %s dropped: %s", object_key, outcome)
        emit_dropped_metric(outcome)
        return
    logger.info("emailed question %s stored for %s", object_key, RECEIVER_USER_ID)
