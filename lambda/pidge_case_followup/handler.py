"""Scheduler target: one reminder to the SIMULATED bank if it has not replied, then tell the case's resident's PWA."""

import logging
import os

import boto3

from shared import cases, channels, graph, payloads, profiles
from shared.enums import ActionOutcome, CaseEvent

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIPT_SENDER = os.environ["RECEIPT_SENDER"]
CASES_ADDRESS = os.environ["CASES_ADDRESS"]

ses_client = boto3.client("sesv2")


def handle(event, context):
    """The resident comes from CASE#.receiverEmail and is resolved before the follow-up is claimed: an unknown resident
    raises (UnknownReceiverError) with nothing written or sent."""
    case_id = event["caseId"]
    case_row = graph.get_case(case_id)
    if case_row is None:
        logger.info("follow-up for %s not sent: %s", case_id, ActionOutcome.UNKNOWN_REQUEST)
        return
    resident = profiles.require_receiver_profile(case_row["receiverEmail"])
    result = cases.send_follow_up(case_id, ses_client, RECEIPT_SENDER, CASES_ADDRESS)
    if result.outcome is not ActionOutcome.RECORDED:
        logger.info("follow-up for %s not sent: %s", case_id, result.outcome)
        return
    channels.push_to_user(resident.receiver_user_id, payloads.case_state_message(result.case_row, graph.list_case_replies(case_id), CaseEvent.FOLLOWED_UP))
