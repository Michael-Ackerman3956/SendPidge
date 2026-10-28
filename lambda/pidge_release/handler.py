"""Scheduler target: at releaseAt, a Yes nobody undid becomes confirmed, both PWAs hear it, Jimin gets the receipt. The
resident comes from the REQUEST# row's receiverEmail, resolved before the write; no profile raises (the invocation retries).
When the office the request was held for is no longer reached (removed from the circle, or a former home's after
re-enrolment: orgs.read_request_contact_row), only the resident hears it, named after their current home; that office gets
no push and no receipt."""

import logging
import os

import boto3

from shared import channels, graph, orgs, payloads, profiles, verdict
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
    contact_row = orgs.read_request_contact_row(result.request_row, resident)
    message = payloads.request_state_message(result.request_row, result.status, orgs.contact_name_for_request(contact_row, resident), False)
    channels.push_to_user(resident.receiver_user_id, message)
    if contact_row is None:
        return  # an office no longer reached: no push and no receipt
    channels.push_to_user(contact_row["contactPwaUserId"], message)
    try:
        verdict.send_receipt(ses_client, RECEIPT_SENDER, contact_row, result.status, resident.resident_name)
    except Exception:
        logger.exception("receipt for %s failed", request_id)
