"""Scheduler target: one reminder to the SIMULATED bank if it has not replied, then tell Grandma's PWA."""

import logging
import os

import boto3

from shared import cases, channels, graph, payloads
from shared.enums import ActionOutcome, CaseEvent

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIPT_SENDER = os.environ["RECEIPT_SENDER"]
CASES_ADDRESS = os.environ["CASES_ADDRESS"]

ses_client = boto3.client("sesv2")


def handle(event, context):
    case_id = event["caseId"]
    result = cases.send_follow_up(case_id, ses_client, RECEIPT_SENDER, CASES_ADDRESS)
    if result.outcome is not ActionOutcome.RECORDED:
        logger.info("follow-up for %s not sent: %s", case_id, result.outcome)
        return
    channels.push_to_user(RECEIVER_USER_ID, payloads.case_state_message(result.case_row, graph.list_case_replies(case_id), CaseEvent.FOLLOWED_UP))
