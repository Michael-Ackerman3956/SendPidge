"""S3 email → Reader (no tools) → policy in code → hold and ask, or warn, or pass."""

import asyncio
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

import boto3

from shared import channels, graph, payloads, schedules
from shared.auth import authentication_reasons
from shared.email_parse import ParsedEmail, parse_eml
from shared.enums import PolicyDecision, ReaderModelChoice, RequestState, WarnReason
from shared.escalation import EscalationFacts, EscalationVerdict, build_escalation_reader, escalate, escalation_fields
from shared.policy import (
    EscalationAnswer, EscalationFailure, PolicyOutcome, ReaderFailure, apply_escalation, decide, needs_escalation,
    verdict_fields_for_request,
)
from shared.reader_agent import EmailVerdict, build_reader, read_email
from shared.sanitize import sanitize_summary

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TEXT_BODY_MAX_CHARACTERS = 20_000
RECEIVER_ADDRESS = os.environ["RECEIVER_ADDRESS"]  # the one monitored address, never To/Cc
RECEIVER_USER_ID = os.environ["RECEIVER_USER_ID"]
RECEIVER_NAME = os.environ["RECEIVER_NAME"]
ESCALATION_ENABLED = os.environ.get("ESCALATION_ENABLED") == "true"
RETRY_SCHEDULER_ENABLED = os.environ.get("RETRY_SCHEDULER_ENABLED") == "true"
RE_ASK_DELAY_SECONDS = int(os.environ.get("RE_ASK_DELAY_SECONDS", "600"))
RETRY_FUNCTION_ARN = os.environ.get("RETRY_FUNCTION_ARN", "")

s3_client = boto3.client("s3")
reader = build_reader(ReaderModelChoice(os.environ["READER_MODEL"]))
escalation_reader = build_escalation_reader(ReaderModelChoice(os.environ["ESCALATION_MODEL"])) if ESCALATION_ENABLED else None


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
    # Same cap as the stored copy: a 10 MB email never becomes a full-context model call.
    capped_email = replace(parsed_email, text_body=parsed_email.text_body[:TEXT_BODY_MAX_CHARACTERS])
    contact_by_name = graph.find_contact(RECEIVER_ADDRESS, capped_email.sender_name)  # gate 1: display name only
    reader_outcome, contact_by_claim = read_and_look_up_claimed_contact(capped_email)
    outcome = decide(reader_outcome, contact_by_name, contact_by_claim, capped_email.sender_address, reasons)
    escalation = None
    if needs_escalation(outcome, ESCALATION_ENABLED):
        outcome, escalation = review_hold(capped_email, outcome, reasons, reader_outcome)
    logger.info("email %s → %s %s %s", object_key, outcome.decision, outcome.warn_reason, list(outcome.reasons))
    if outcome.decision is PolicyDecision.HOLD:
        hold_and_ask(object_key, email_row, reader_outcome, outcome, escalation)
    else:
        graph.record_email_outcome(object_key, outcome.decision, outcome.warn_reason, outcome.reasons, None)
        if escalation is not None:
            graph.record_email_escalation(object_key, escalation_fields(escalation))
        push_email_state(email_row, outcome, request_row=None, contact_name=None)


def read_and_look_up_claimed_contact(parsed_email: ParsedEmail) -> tuple[EmailVerdict | ReaderFailure, graph.Contact | None]:
    """Any Reader or graph failure becomes ReaderFailure (WARN unreadable, or gate 1's hold) plus a logged error; never a PASS."""
    try:
        verdict = read_email(reader, parsed_email)
        return verdict, graph.find_contact(RECEIVER_ADDRESS, verdict.claimed_person)
    except Exception as error:
        logger.exception("reader or contact lookup failed")
        return ReaderFailure(str(error)), None


def review_hold(
    parsed_email: ParsedEmail, outcome: PolicyOutcome, reasons: list, reader_outcome: EmailVerdict | ReaderFailure
) -> tuple[PolicyOutcome, EscalationVerdict | EscalationFailure]:
    """The second reviewer sees code's facts and the body; code applies its answer (gate 1 is never downgraded)."""
    facts = EscalationFacts(
        gate_fired=outcome.gate,
        display_name_matches_contact=outcome.gate is not None and outcome.contact is not None
        and " ".join(parsed_email.sender_name.split()).casefold() == " ".join(outcome.contact.contact_name.split()).casefold(),
        address_known=outcome.is_address_known,
        authentication="; ".join(parsed_email.pidge_auth_header_values) or "none",
        reasons=tuple(reasons),
        reader_verdict=None if isinstance(reader_outcome, ReaderFailure) else reader_outcome,
    )
    escalation = escalate(escalation_reader, parsed_email, facts)
    if isinstance(escalation, EscalationFailure):
        return apply_escalation(outcome, escalation), escalation
    answer = EscalationAnswer(escalation.decision, graph.find_contact(RECEIVER_ADDRESS, escalation.claimed_person))
    return apply_escalation(outcome, answer), escalation


def hold_and_ask(
    email_id: str, email_row: dict, reader_outcome: EmailVerdict | ReaderFailure, outcome: PolicyOutcome,
    escalation: EscalationVerdict | EscalationFailure | None,
) -> None:
    """The hold reaches Grandma before any channel is tried, so a failed channel never undoes it."""
    contact = outcome.contact
    summary = "" if isinstance(reader_outcome, ReaderFailure) else sanitize_summary(reader_outcome.summary)
    verdict_fields = verdict_fields_for_request(reader_outcome, summary)
    request_row = graph.create_request(
        email_id, RECEIVER_ADDRESS, contact, verdict_fields, outcome.friction,
        escalation_fields(escalation) if escalation is not None else None,
    )
    request_id = graph.request_id_of(request_row)
    graph.record_email_outcome(email_id, outcome.decision, None, outcome.reasons, request_id)
    push_email_state(email_row, outcome, request_row, contact.contact_name)
    channels_sent = asyncio.run(channels.ask_on_every_channel(request_row, contact, RECEIVER_NAME))
    graph.record_channels_sent(request_id, channels_sent)
    if not channels_sent:
        unreachable = PolicyOutcome(PolicyDecision.WARN, WarnReason.UNREACHABLE, outcome.reasons)
        push_email_state(email_row, unreachable, request_row, contact.contact_name)  # still held, never a release
    schedule_re_ask(request_id)


def schedule_re_ask(request_id: str) -> None:
    """Books the first re-ask timer; with the flag off nothing marks timeout and the PWA shows the shield on its own."""
    if not RETRY_SCHEDULER_ENABLED:
        return
    try:
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=RE_ASK_DELAY_SECONDS)
        schedules.schedule_one_time_invocation(f"retry-{request_id}-1", fire_at, RETRY_FUNCTION_ARN, {"requestId": request_id, "attempt": 1})
    except Exception:
        logger.exception("re-ask schedule for %s failed; the hold stays without a retry", request_id)


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
    status = RequestState(request_row["status"]) if request_row else None
    message = payloads.email_state_message(decided_row, request_row, contact_name, status)
    delivered_count = channels.push_to_user(RECEIVER_USER_ID, message)
    logger.info("pushed %s state to %d receiver connection(s)", outcome.decision, delivered_count)
