"""One inbound item for one resident: links and lists → Reader (no tools) → policy in code → hold and ask, or warn, or pass.
pidge-reader hands it each accepted email, pidge-inbound each text or media message sent to Pidge's WhatsApp, RCS or SMS number."""

import asyncio
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from shared import channels, graph, hourly_counters, inbox_rows, lists, orgs, payloads, schedules, sources
from shared.metrics import emit_count_metric
from shared.auth import authentication_reasons
from shared import links as link_checks
from shared.email_parse import ParsedEmail
from shared.enums import Channel, PolicyDecision, ReaderModelChoice, RequestState, WarnReason
from shared.list_enums import ScamListCoverageState
from shared.escalation import EscalationFacts, EscalationVerdict, build_escalation_reader, escalate, escalation_fields
from shared.inbound_texts import ReceivedText
from shared.messenger_enums import MessageDirection
from shared.policy import (
    EscalationAnswer, EscalationFailure, PolicyOutcome, ReaderFailure, apply_escalation, decide, needs_escalation,
    verdict_fields_for_request,
)
from shared.profiles import ReceiverProfile
from shared.reach_points import reach_channel_of
from shared.reader_agent import EmailVerdict, build_reader, read_email
from shared.sanitize import sanitize_summary

READING_PUSH_FAILED_METRIC_NAME = "ReadingPushFailed"
NO_TEXT_TO_READ = "no text to read"  # the ReaderFailure of a media message: the Reader is never called on it

logger = logging.getLogger(__name__)

TEXT_BODY_MAX_CHARACTERS = 20_000
ESCALATION_ENABLED = os.environ.get("ESCALATION_ENABLED") == "true"
RETRY_SCHEDULER_ENABLED = os.environ.get("RETRY_SCHEDULER_ENABLED") == "true"
RETRY_FUNCTION_ARN = os.environ.get("RETRY_FUNCTION_ARN", "")
ASKS_PER_CONTACT_PER_HOUR = int(os.environ.get("ASKS_PER_CONTACT_PER_HOUR", "3"))  # the ask cap; past it the hold stays silent

reader = build_reader(ReaderModelChoice(os.environ["READER_MODEL"]))
escalation_reader = build_escalation_reader(ReaderModelChoice(os.environ["ESCALATION_MODEL"])) if ESCALATION_ENABLED else None
list_store = lists.DynamoListStore()


@dataclass(frozen=True)
class InboundMessage:
    channel: Channel  # where it came in: keys its row (inbox_rows) and becomes REQUEST#.inboundChannel
    item_id: str  # the row's id: inbox/<messageId> for an email, <channel>#<providerMessageId> for a text
    content: ParsedEmail
    is_content_readable: bool = True  # False for media: the Reader is skipped and the outcome is gate 1 or WARN unreadable
    sent_at: str | None = None  # the provider's send time; set on WhatsApp, where it opens the thread's reply window


def inbound_message_for_text(received: ReceivedText) -> InboundMessage:
    """A text as the pipeline reads it: the WhatsApp profile name is the display name gate 1 and the Reader see (never
    stored); no domain, subject, authentication or links. Media carries no text and is marked unreadable."""
    content = ParsedEmail(sender_name=received.sender_profile_name, sender_address=received.sender_address, sender_domain="",
                          subject="", text_body="" if received.text is None else received.text)
    return InboundMessage(received.channel, inbox_rows.message_item_id(received.channel, received.provider_message_id), content,
                          is_content_readable=received.text is not None, sent_at=received.sent_at)


def run_pipeline(receiver: ReceiverProfile, message: InboundMessage) -> None:
    contacts = contacts_on_channel(message.channel, orgs.contacts_with_live_office(  # the own home's office from its live PROFILE
        receiver, graph.list_contacts(receiver.receiver_address)))  # one query per item; every name match below reads this list
    link_assessments, inbound_row = assess_links_and_build_row(receiver, message, contacts)
    if not inbox_rows.claim_row(message.channel, message.item_id, inbound_row):
        if not inbox_rows.reclaim_abandoned_row(message.channel, message.item_id, datetime.now(timezone.utc)):
            logger.info("duplicate event for %s; already processed or still running", message.item_id)
            return
        logger.warning("%s was claimed but never decided; this retry runs it again", message.item_id)
    record_whatsapp_window(receiver, message)
    push_reading(receiver, message, inbound_row)
    outcome, reader_outcome, escalation = decide_inbound(message, link_assessments, contacts)
    logger.info("%s → %s %s %s", message.item_id, outcome.decision, outcome.warn_reason, list(outcome.reasons))
    if outcome.decision is PolicyDecision.HOLD:
        hold_and_ask(receiver, message.channel, message.item_id, inbound_row, reader_outcome, outcome, escalation)
        return
    inbox_rows.record_outcome(message.channel, message.item_id, outcome.decision, outcome.warn_reason, outcome.reasons, None)
    if escalation is not None:
        inbox_rows.record_escalation(message.channel, message.item_id, escalation_fields(escalation))
    push_state(receiver, message.channel, inbound_row, outcome, request_row=None, contact_name=None)


def contacts_on_channel(channel: Channel, contacts: list[graph.Contact]) -> list[graph.Contact]:
    """Who a sender is known as. Email keeps each contact's known sender addresses; on a text channel a contact is known only
    by the checked numbers Pidge may ask on its reach channel (an SMS number is the RCS point), so gate 1 and the lookalike
    check judge the number and a PENDING number is a stranger."""
    if channel is Channel.EMAIL:
        return contacts
    reach_channel = reach_channel_of(channel)
    return [replace(contact, known_addresses=contact.addresses_to_ask(reach_channel)) for contact in contacts]


def assess_links_and_build_row(
    receiver: ReceiverProfile, message: InboundMessage, contacts: list[graph.Contact],
) -> tuple[list[link_checks.LinkAssessment], dict]:
    """An email's links are checked before the claim, so EMAIL# stores LINK_LISTED and the scam-list coverage in one write (a
    failed read answers unavailable). A text has no links (URLs in plain text are not extracted) and no coverage."""
    parsed_email = message.content
    if message.channel is not Channel.EMAIL:
        return [], build_message_row(receiver, message, contacts)
    link_assessments = link_checks.assess_links(parsed_email.links, graph.domains_of(contacts))
    scam_list_check = sources.check_against_live_sources(
        link_checks.list_indicators_of(link_assessments, parsed_email.sender_domain), graph.now_timestamp())
    link_assessments = link_checks.mark_listed_links(link_assessments, scam_list_check)
    return link_assessments, build_email_row(receiver, message.item_id, parsed_email, link_assessments, scam_list_check.coverage)


def decide_inbound(
    message: InboundMessage, link_assessments: list[link_checks.LinkAssessment], contacts: list[graph.Contact],
) -> tuple[PolicyOutcome, EmailVerdict | ReaderFailure, EscalationVerdict | EscalationFailure | None]:
    """Reasons in code, the Reader, policy, then the second reviewer's answer. Authentication is read on email only (a text
    carries none); content with nothing to read never reaches the Reader."""
    parsed_email = message.content
    reasons = authentication_reasons(parsed_email.pidge_auth_header_values) if message.channel is Channel.EMAIL else []
    reasons.extend(link_checks.link_reasons(link_assessments, parsed_email))
    reasons.extend(lists.sender_reasons_or_lookup_failure(
        list_store, parsed_email.sender_address, parsed_email.sender_domain, parsed_email.sender_name, contacts))
    # Same cap as the stored copy: a 10 MB email never becomes a full-context model call.
    capped_email = replace(parsed_email, text_body=parsed_email.text_body[:TEXT_BODY_MAX_CHARACTERS])
    contact_by_name = graph.find_contact_among(contacts, capped_email.sender_name)  # gate 1: display name only
    if message.is_content_readable:
        reader_outcome, contact_by_claim = read_and_look_up_claimed_contact(capped_email, contacts)
    else:
        reader_outcome, contact_by_claim = ReaderFailure(NO_TEXT_TO_READ), None  # media: gate 1's hold or WARN unreadable
    outcome = decide(reader_outcome, contact_by_name, contact_by_claim, capped_email.sender_address, reasons)
    escalation = None
    if needs_escalation(outcome, ESCALATION_ENABLED):
        outcome, escalation = review_hold(capped_email, outcome, reasons, reader_outcome, contacts)
    return outcome, reader_outcome, escalation


def record_whatsapp_window(receiver: ReceiverProfile, message: InboundMessage) -> None:
    """A WhatsApp message opens Meta's 24-h reply window: its send time is stamped on the thread (THREAD#, newest wins)."""
    if message.channel is not Channel.WHATSAPP:
        return
    if message.sent_at is None:
        raise ValueError(f"WhatsApp message {message.item_id} has no send time")
    thread_key = inbox_rows.thread_key_for(message.channel, message.content.sender_address)
    inbox_rows.record_last_inbound(receiver.receiver_address, thread_key, message.sent_at)


def read_and_look_up_claimed_contact(
    parsed_email: ParsedEmail, contacts: list[graph.Contact],
) -> tuple[EmailVerdict | ReaderFailure, graph.Contact | None]:
    """Any Reader failure becomes ReaderFailure (WARN unreadable, or gate 1's hold) plus a logged error; never a PASS."""
    try:
        verdict = read_email(reader, parsed_email)
        return verdict, graph.find_contact_among(contacts, verdict.claimed_person)
    except Exception as error:
        logger.exception("reader or contact lookup failed")
        return ReaderFailure(str(error)), None


def review_hold(
    parsed_email: ParsedEmail, outcome: PolicyOutcome, reasons: list, reader_outcome: EmailVerdict | ReaderFailure,
    contacts: list[graph.Contact],
) -> tuple[PolicyOutcome, EscalationVerdict | EscalationFailure]:
    """The second reviewer sees code's facts and the body; code applies its answer (gate 1 is never downgraded)."""
    facts = EscalationFacts(
        gate_fired=outcome.gate,
        display_name_matches_contact=outcome.gate is not None and outcome.contact is not None
        and graph.find_contact_among([outcome.contact], parsed_email.sender_name) is not None,
        address_known=outcome.is_address_known,
        authentication="; ".join(parsed_email.pidge_auth_header_values) or "none",
        reasons=tuple(reasons),
        reader_verdict=None if isinstance(reader_outcome, ReaderFailure) else reader_outcome,
    )
    escalation = escalate(escalation_reader, parsed_email, facts)
    if isinstance(escalation, EscalationFailure):
        return apply_escalation(outcome, escalation), escalation
    answer = EscalationAnswer(escalation.decision, graph.find_contact_among(contacts, escalation.claimed_person))
    return apply_escalation(outcome, answer), escalation


def hold_and_ask(
    receiver: ReceiverProfile, channel: Channel, item_id: str, inbound_row: dict, reader_outcome: EmailVerdict | ReaderFailure,
    outcome: PolicyOutcome, escalation: EscalationVerdict | EscalationFailure | None,
) -> None:
    """The hold reaches the resident before any channel is tried, so a failed channel never undoes it."""
    contact = outcome.contact
    summary = "" if isinstance(reader_outcome, ReaderFailure) else sanitize_summary(reader_outcome.summary)
    verdict_fields = verdict_fields_for_request(reader_outcome, summary)
    request_row = graph.create_request(
        item_id, receiver.receiver_address, contact, verdict_fields, outcome.friction,
        escalation_fields(escalation) if escalation is not None else None, channel,
    )
    request_id = graph.request_id_of(request_row)
    inbox_rows.record_outcome(channel, item_id, outcome.decision, None, outcome.reasons, request_id)
    push_state(receiver, channel, inbound_row, outcome, request_row, contact.contact_name)
    if not hourly_counters.take_ask_slot(contact.contact_key, receiver.receiver_address, ASKS_PER_CONTACT_PER_HOUR):
        # The hold stays; no channel is tried and no re-ask is booked, so a flood of asks never reaches the contact.
        logger.info("ask cap reached for %s; %s held without an ask", contact.contact_key, request_id)
        graph.record_ask_capped(request_id)
        keep_held_with_warning(receiver, channel, item_id, inbound_row, request_row, outcome, WarnReason.ASK_CAPPED,
                               contact.contact_name)
        return
    channels_sent = asyncio.run(channels.ask_on_every_channel(request_row, contact, receiver.resident_name))
    graph.record_channels_sent(request_id, channels_sent)
    if not channels_sent:
        keep_held_with_warning(receiver, channel, item_id, inbound_row, request_row, outcome, WarnReason.UNREACHABLE,
                               contact.contact_name)
    schedule_re_ask(request_id)


def keep_held_with_warning(
    receiver: ReceiverProfile, channel: Channel, item_id: str, inbound_row: dict, request_row: dict, outcome: PolicyOutcome,
    warn_reason: WarnReason, contact_name: str,
) -> None:
    """The request stays pending and the item held, but its EMAIL# / MESSAGE# row and the receiver both get the WARN, so a
    reload shows the same calm banner as the live push instead of a countdown for an ask that never went out. Never a release."""
    warned = PolicyOutcome(PolicyDecision.WARN, warn_reason, outcome.reasons)
    inbox_rows.record_outcome(channel, item_id, warned.decision, warned.warn_reason, warned.reasons, graph.request_id_of(request_row))
    push_state(receiver, channel, inbound_row, warned, request_row, contact_name)


def schedule_re_ask(request_id: str) -> None:
    """Books the first re-ask timer; with the flag off nothing marks timeout and the PWA shows the shield on its own."""
    if not RETRY_SCHEDULER_ENABLED:
        return
    try:
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=graph.HOLD_DURATION_SECONDS)
        schedules.schedule_one_time_invocation(f"retry-{request_id}-1", fire_at, RETRY_FUNCTION_ARN, {"requestId": request_id, "attempt": 1})
    except Exception:
        logger.exception("re-ask schedule for %s failed; the hold stays without a retry", request_id)


def build_email_row(
    receiver: ReceiverProfile, object_key: str, parsed_email: ParsedEmail, link_assessments: list[link_checks.LinkAssessment],
    scam_list_coverage: ScamListCoverageState,
) -> dict:
    received_at = graph.now_timestamp()
    return {
        "s3Key": object_key,
        "senderName": parsed_email.sender_name,
        "senderAddress": parsed_email.sender_address,
        "subject": parsed_email.subject,
        "textBody": parsed_email.text_body[:TEXT_BODY_MAX_CHARACTERS],
        "links": [assessment.as_row() for assessment in link_assessments],
        "scamListCoverage": str(scam_list_coverage),  # unavailable: a live feed is stale or the lists could not be read
        "imageCount": parsed_email.image_count,  # tracking pixels included; counted, never a reason on its own (D7)
        "visibleHtmlCharacters": parsed_email.visible_html_characters,
        "receivedAt": received_at,
        "receiverEmail": receiver.receiver_address,
        "quarantined": False,
        graph.USER_INDEX_PARTITION_KEY: graph.user_index_partition(receiver.receiver_user_id, graph.EMAIL_ROW_KIND),
        graph.USER_INDEX_SORT_KEY: received_at,
    }


def build_message_row(receiver: ReceiverProfile, message: InboundMessage, contacts: list[graph.Contact]) -> dict:
    """A text's MESSAGE# row. senderName is the contact whose checked number sent it (contacts_on_channel), else "": the
    WhatsApp profile name is the sender's own choice and is never stored."""
    parsed_email = message.content
    received_at = graph.now_timestamp()
    return {
        "channel": str(message.channel),
        "direction": str(MessageDirection.IN),
        "threadKey": inbox_rows.thread_key_for(message.channel, parsed_email.sender_address),
        "providerMessageId": message.item_id.removeprefix(f"{message.channel}#"),
        "senderAddress": parsed_email.sender_address,
        "senderName": next((contact.contact_name for contact in contacts if contact.is_known_address(parsed_email.sender_address)), ""),
        "textBody": parsed_email.text_body[:TEXT_BODY_MAX_CHARACTERS],
        "links": [],
        "receivedAt": received_at,
        "receiverEmail": receiver.receiver_address,
        "quarantined": False,
        graph.USER_INDEX_PARTITION_KEY: graph.user_index_partition(receiver.receiver_user_id, inbox_rows.MESSAGE_ROW_KIND),
        graph.USER_INDEX_SORT_KEY: received_at,
    }


def push_reading(receiver: ReceiverProfile, message: InboundMessage, inbound_row: dict) -> None:
    """"Pidge is reading…" on the resident's screen while the Reader runs; a failed push is logged and never delays the verdict."""
    try:
        delivered_count = channels.push_to_user(receiver.receiver_user_id, reading_message_for(message.channel, inbound_row))
        logger.info("pushed reading state to %d receiver connection(s)", delivered_count)
    except Exception:
        logger.exception("reading push for %s failed; the verdict push follows", message.item_id)
        emit_count_metric(READING_PUSH_FAILED_METRIC_NAME)


def reading_message_for(channel: Channel, inbound_row: dict) -> dict:
    """email_state for an EMAIL# row, message_state for a MESSAGE# row; either without the body."""
    if channel is Channel.EMAIL:
        return payloads.email_reading_message(inbound_row)
    return payloads.message_reading_message(inbound_row)


def push_state(
    receiver: ReceiverProfile, channel: Channel, inbound_row: dict, outcome: PolicyOutcome, request_row: dict | None,
    contact_name: str | None,
) -> None:
    decided_row = {
        **inbound_row,
        "state": outcome.decision,
        "warnReason": outcome.warn_reason,
        "reasons": list(outcome.reasons),
    }
    status = RequestState(request_row["status"]) if request_row else None
    state_message_for = payloads.email_state_message if channel is Channel.EMAIL else payloads.message_state_message
    delivered_count = channels.push_to_user(receiver.receiver_user_id, state_message_for(decided_row, request_row, contact_name, status))
    logger.info("pushed %s state to %d receiver connection(s)", outcome.decision, delivered_count)
