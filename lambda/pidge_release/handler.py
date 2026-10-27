"""Scheduler target: at releaseAt, a Yes nobody undid becomes confirmed, both PWAs hear it, Jimin gets the receipt. The
resident comes from the REQUEST# row's receiverEmail, resolved before the write; no profile raises (the invocation retries)."""

import logging
import os

import boto3

from shared import channels, graph, payloads, profiles, verdict
from shared.enums import ActionOutcome

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIPT_SENDER = os.environ["RECEIPT_SENDER"]

ses_client = boto3.client("sesv2")


def handle(event, context):
    request_id = event["requestId"]
    request_row = graph.get_request(request_id)
    if request_row is None:
        logger.info("release of %s: no such request", request_id)
        return
    resident = profiles.require_receiver_profile(request_row["receiverEmail"])
    result = verdict.release_if_still_pending(request_id)
    if result.outcome is not ActionOutcome.RECORDED:
        logger.info("release of %s not applied: %s", request_id, result.outcome)
        return
    message = payloads.request_state_message(result.request_row, result.status, result.contact_row["contactName"], False)
    channels.push_to_user(resident.receiver_user_id, message)
    channels.push_to_user(result.contact_row["contactPwaUserId"], message)
    try:
        verdict.send_receipt(ses_client, RECEIPT_SENDER, result.contact_row, result.status, resident.resident_name)
    except Exception:
        logger.exception("receipt for %s failed", request_id)
