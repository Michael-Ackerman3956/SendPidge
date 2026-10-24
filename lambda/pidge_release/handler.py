"""Scheduler target: at releaseAt, a Yes nobody undid becomes confirmed, both PWAs hear it, Jimin gets the receipt."""

import logging
import os

import boto3

from shared import channels, payloads, verdict
from shared.enums import ActionOutcome

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIVER_NAME = os.environ["RECEIVER_NAME"]
RECEIPT_SENDER = os.environ["RECEIPT_SENDER"]

ses_client = boto3.client("sesv2")


def handle(event, context):
    request_id = event["requestId"]
    result = verdict.release_if_still_pending(request_id)
    if result.outcome is not ActionOutcome.RECORDED:
        logger.info("release of %s not applied: %s", request_id, result.outcome)
        return
    message = payloads.request_state_message(result.request_row, result.status, result.contact_row["contactName"], False)
    channels.push_to_user(RECEIVER_USER_ID, message)
    channels.push_to_user(result.contact_row["contactPwaUserId"], message)
    try:
        verdict.send_receipt(ses_client, RECEIPT_SENDER, result.contact_row, result.status, RECEIVER_NAME)
    except Exception:
        logger.exception("receipt for %s failed", request_id)
