"""S3 cases/ → a bank reply. Matched by the subject token, stored verbatim, shown to Grandma (in-app + Web Push), Jimin nudged. Never the Reader."""

import logging
import os
from urllib.parse import unquote_plus

import boto3

from shared import cases, channels, graph, payloads, web_push
from shared.metrics import emit_count_metric
from shared.auth import authentication_reasons
from shared.email_parse import parse_eml
from shared.enums import ActionOutcome, CaseEvent, PushNotice

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIVER_NAME = os.environ["RECEIVER_NAME"]
RECEIVER_ADDRESS = os.environ["RECEIVER_ADDRESS"]
CONTACT_NUDGE_TEXT = f"{RECEIVER_NAME}'s bank replied about the payment she reported. Pidge is on it."
REJECTED_METRIC_NAME = "CaseReplyRejected"

s3_client = boto3.client("s3")


def handle(event, context):
    for record in event["Records"]:
        handle_case_reply_object(record["s3"]["bucket"]["name"], unquote_plus(record["s3"]["object"]["key"]))


def emit_rejected_metric(outcome: ActionOutcome) -> None:
    emit_count_metric(REJECTED_METRIC_NAME, "Outcome", str(outcome))


def handle_case_reply_object(bucket_name: str, object_key: str) -> None:
    raw_email = s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read()
    parsed = parse_eml(raw_email)
    reply = cases.InboundCaseReply(
        object_key, parsed.sender_address, parsed.subject, parsed.text_body,
        tuple(authentication_reasons(parsed.pidge_auth_header_values)),
    )
    result = cases.record_bank_reply(reply)
    if result.outcome is not ActionOutcome.RECORDED:
        logger.warning("case reply %s dropped: %s", object_key, result.outcome)
        emit_rejected_metric(result.outcome)
        return
    case_id = graph.case_id_of(result.case_row)
    channels.push_to_user(RECEIVER_USER_ID, payloads.case_state_message(result.case_row, graph.list_case_replies(case_id), CaseEvent.BANK_REPLIED))
    notified_count = web_push.send_notice(RECEIVER_USER_ID, PushNotice.BANK_REPLIED)  # lock-screen notice; fixed text
    logger.info("bank reply on %s: web push accepted by %d browser(s)", case_id, notified_count)
    request_row = graph.get_request(result.case_row["requestId"])
    contact_row = graph.get_contact_row(RECEIVER_ADDRESS, request_row["contactSK"]) if request_row else None
    if contact_row:
        channels.push_to_user(contact_row["contactPwaUserId"], payloads.contact_notice_message(CONTACT_NUDGE_TEXT))  # fixed text, never the reply
