"""S3 email → Reader (no tools) → policy in code → hold and ask, or warn, or pass."""

import asyncio
import logging
import os
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3

from shared import channels, graph, payloads
from shared.auth import authentication_reasons
from shared.email_parse import ParsedEmail, parse_eml
from shared.enums import PolicyDecision, ReaderModelChoice, WarnReason
from shared.policy import PolicyOutcome, ReaderFailure, decide
from shared.reader_agent import EmailVerdict, build_reader, read_email
from shared.sanitize import sanitize_summary

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TEXT_BODY_MAX_CHARACTERS = 20_000
RECEIVER_ADDRESS = os.environ["RECEIVER_ADDRESS"]  # the one monitored address, never To/Cc
RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIVER_NAME = os.environ["RECEIVER_NAME"]

s3_client = boto3.client("s3")
reader = build_reader(ReaderModelChoice(os.environ["READER_MODEL"]))


def handle(event, context):
    for record in event["Records"]:
        process_email_object(record["s3"]["bucket"]["name"], unquote_plus(record["s3"]["object"]["key"]))


def process_email_object(bucket_name: str, object_key: str) -> None:
    raw_email = s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read()
    parsed_email = parse_eml(raw_email)
    email_row = build_email_row(object_key, parsed_email)
    if not graph.claim_email(object_key, email_row):
        logger.info("duplicate event for %s; already processed", object_key)
        return
    reasons = authentication_reasons(parsed_email.pidge_auth_header_values)
    reader_outcome, contact = read_and_look_up_contact(parsed_email)
    outcome = decide(reader_outcome, contact is not None, reasons)
    logger.info("email %s → %s %s %s", object_key, outcome.decision, outcome.warn_reason, list(outcome.reasons))
    if outcome.decision is PolicyDecision.HOLD:
        hold_and_ask(object_key, email_row, reader_outcome, contact, outcome)
    else:
        graph.record_email_outcome(object_key, outcome.decision, outcome.warn_reason, outcome.reasons, None)
        push_email_state(email_row, outcome, request_row=None, contact_name=None)


def read_and_look_up_contact(parsed_email: ParsedEmail) -> tuple[EmailVerdict | ReaderFailure, graph.Contact | None]:
    """Any Reader or graph failure becomes ReaderFailure (WARN unreadable) plus a logged error; never a PASS."""
    try:
        verdict = read_email(reader, parsed_email)
        return verdict, graph.find_contact(RECEIVER_ADDRESS, verdict.claimed_person)
    except Exception as error:
        logger.exception("reader or contact lookup failed")
        return ReaderFailure(str(error)), None


def hold_and_ask(
    email_id: str, email_row: dict, verdict: EmailVerdict, contact: graph.Contact, outcome: PolicyOutcome
) -> None:
    """The hold reaches Grandma before any channel is tried, so a failed channel never undoes it."""
    verdict_fields = {**verdict.model_dump(mode="json"), "summary": sanitize_summary(verdict.summary)}
    request_row = graph.create_request(email_id, RECEIVER_ADDRESS, contact, verdict_fields)
    request_id = graph.request_id_of(request_row)
    graph.record_email_outcome(email_id, outcome.decision, None, outcome.reasons, request_id)
    push_email_state(email_row, outcome, request_row, contact.contact_name)
    channels_sent = asyncio.run(channels.ask_on_every_channel(request_row, contact, RECEIVER_NAME))
    graph.record_channels_sent(request_id, channels_sent)
    if not channels_sent:
        unreachable = PolicyOutcome(PolicyDecision.WARN, WarnReason.UNREACHABLE, outcome.reasons)
        push_email_state(email_row, unreachable, request_row, contact.contact_name)  # still held, never a release


def build_email_row(object_key: str, parsed_email: ParsedEmail) -> dict:
    received_at = graph.iso_timestamp(datetime.now(timezone.utc))
    return {
        "s3Key": object_key,
        "senderName": parsed_email.sender_name,
        "senderAddress": parsed_email.sender_address,
        "subject": parsed_email.subject,
        "textBody": parsed_email.text_body[:TEXT_BODY_MAX_CHARACTERS],
        "receivedAt": received_at,
        "receiverEmail": RECEIVER_ADDRESS,
        "quarantined": False,
        graph.USER_INDEX_PARTITION_KEY: graph.user_index_partition(RECEIVER_USER_ID, graph.EMAIL_ROW_KIND),
        graph.USER_INDEX_SORT_KEY: received_at,
    }


def push_email_state(
    email_row: dict, outcome: PolicyOutcome, request_row: dict | None, contact_name: str | None
) -> None:
    decided_row = {
        **email_row,
        "state": outcome.decision,
        "warnReason": outcome.warn_reason,
        "reasons": list(outcome.reasons),
    }
    message = payloads.email_state_message(decided_row, request_row, contact_name)
    delivered_count = channels.push_to_user(RECEIVER_USER_ID, message)
    logger.info("pushed %s state to %d receiver connection(s)", outcome.decision, delivered_count)
