"""Mail Manager invokes this per accepted email (stored under inbox/<messageId>): the envelope recipient → the enrolled
resident → Reader (no tools) → policy in code → hold and ask, or warn, or pass."""

import logging
import os
import re
import time
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from shared import pipeline
from shared.metrics import emit_count_metric
from shared.email_parse import parse_eml
from shared.enums import Channel
from shared.profiles import (
    UNKNOWN_RECEIVER_METRIC_NAME, UnknownReceiverError, normalize_receiver_address, require_receiver_profile,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EMAIL_BUCKET = os.environ["EMAIL_BUCKET"]
INBOX_PREFIX = "inbox/"  # Mail Manager's store-raw-email rule writes here (infra/lib/mail-ingress.ts INBOX_PREFIX)
SES_EVENT_SOURCE = "aws:ses"
MESSAGE_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,200}")  # used with fullmatch; "." and ".." are refused separately
RAW_EMAIL_FETCH_RETRY_DELAYS_SECONDS = (1, 2, 4)  # the S3 write may land after the invoke; a later miss raises
NO_SUCH_KEY_ERROR_CODE = "NoSuchKey"

s3_client = boto3.client("s3")


@dataclass(frozen=True)
class InboundDelivery:
    bucket_name: str
    object_key: str  # inbox/<messageId>, also the EMAIL# id
    receiver_address: str  # the envelope recipient, normalized; never To/Cc or model output


def handle(event, context):
    delivery = resolve_inbound_delivery(event)
    if delivery is None:
        return
    process_inbound_delivery(delivery)


def resolve_inbound_delivery(event) -> InboundDelivery | None:
    """The only code that reads the invoke event: one SES record, its messageId and its one envelope recipient. A malformed
    event raises (retried, then the dead-letter queue); a recipient count other than one counts UnknownReceiver and is
    dropped. Never reads mail.destination, commonHeaders or headers."""
    [record] = event["Records"]
    if record["eventSource"] != SES_EVENT_SOURCE:
        raise ValueError("not an SES record")
    message_id = record["ses"]["mail"]["messageId"]
    if not isinstance(message_id, str) or not MESSAGE_ID_PATTERN.fullmatch(message_id) or message_id in (".", ".."):
        raise ValueError("messageId is not a safe object name")
    recipients = record["ses"]["receipt"]["recipients"]
    if not isinstance(recipients, list) or len(recipients) != 1 or not isinstance(recipients[0], str):
        emit_count_metric(UNKNOWN_RECEIVER_METRIC_NAME)
        logger.warning("message %s does not have exactly one envelope recipient; dropped, nothing written", message_id)
        return None
    return InboundDelivery(EMAIL_BUCKET, INBOX_PREFIX + message_id, normalize_receiver_address(recipients[0]))


def process_inbound_delivery(delivery: InboundDelivery) -> None:
    """The resident first: an address with no complete RECEIVER# profile is dropped (counted by require_receiver_profile)
    before the email is fetched or any row is written."""
    try:
        receiver = require_receiver_profile(delivery.receiver_address)
    except UnknownReceiverError:
        logger.warning("no enrolled resident for %s; dropped, nothing written", delivery.object_key)
        return
    raw_email = fetch_raw_email(delivery.bucket_name, delivery.object_key)
    pipeline.run_pipeline(receiver, pipeline.InboundMessage(Channel.EMAIL, delivery.object_key, parse_eml(raw_email)))


def fetch_raw_email(bucket_name: str, object_key: str) -> bytes:
    """Retries a missing object after 1, 2 and 4 s; still missing, or any other error, raises."""
    for delay_seconds in RAW_EMAIL_FETCH_RETRY_DELAYS_SECONDS:
        try:
            return s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read()
        except ClientError as error:
            if error.response["Error"]["Code"] != NO_SUCH_KEY_ERROR_CODE:
                raise
            logger.info("%s not stored yet; retrying in %d s", object_key, delay_seconds)
            time.sleep(delay_seconds)
    return s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read()

