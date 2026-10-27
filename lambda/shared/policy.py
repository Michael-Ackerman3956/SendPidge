"""The hold / warn / pass decision: two gates, then the second reviewer's answer applied. Pure code: no model, no network."""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from shared.enums import LINK_REASON_CODES, EscalationDecision, Friction, GateFired, Intent, PolicyDecision, ReasonCode, WarnReason
from shared.graph import Contact
from shared.reader_agent import EmailVerdict
from shared.sanitize import sanitize_summary

HOLD_CONFIDENCE_THRESHOLD = 0.6
DOUBLE_TAP_INTENTS = (Intent.MONEY, Intent.CODE)
UNREADABLE_SUMMARY = "a message Pidge could not read"
CLAIMED_PERSON_MAX_LENGTH = 60


@dataclass(frozen=True)
class ReaderFailure:
    """The Reader returned no valid verdict. Never released; held only by gate 1."""

    error_message: str


@dataclass(frozen=True)
class PolicyOutcome:
    decision: PolicyDecision
    warn_reason: WarnReason | None
    reasons: tuple[ReasonCode, ...]
    contact: Contact | None = None  # who gets asked on a HOLD
    gate: GateFired | None = None
    friction: Friction | None = None
    is_address_known: bool = False  # the sender address is one the asked contact is known to write from


@dataclass(frozen=True)
class EscalationFailure:
    """The second reviewer could not answer (no access, throttled, bad schema). The hold stays."""

    error_message: str


@dataclass(frozen=True)
class EscalationAnswer:
    """What apply_escalation needs from the second reviewer; the full verdict is stored separately."""

    decision: EscalationDecision
    contact_by_claim: Contact | None  # graph lookup of the reviewer's claimed_person, same rule as the Reader's


def stored_claimed_person(claimed_person: str | None) -> str | None:
    """The Reader's claimed name as the tool agents read it: sanitised like the summary and capped; None when nothing is left."""
    if claimed_person is None:
        return None
    return sanitize_summary(claimed_person)[:CLAIMED_PERSON_MAX_LENGTH].rstrip() or None


def verdict_fields_for_request(reader_outcome: EmailVerdict | ReaderFailure, summary: str) -> dict:
    """What the REQUEST# row stores as `verdict`; a failed Reader leaves a neutral verdict with the unreadable summary.
    claimed_person is stored sanitised and capped; contact matching already ran on the raw value."""
    if isinstance(reader_outcome, ReaderFailure):
        return {"claimed_person": None, "intent": str(Intent.NONE), "confidence": 0.0, "summary": UNREADABLE_SUMMARY}
    return {**reader_outcome.model_dump(mode="json"), "claimed_person": stored_claimed_person(reader_outcome.claimed_person),
            "summary": summary}


def is_impersonator_of(contact_by_name: Contact | None, sender_address: str) -> bool:
    """Gate 1: the From display name is a contact's and the address is not one they are known to write from."""
    return contact_by_name is not None and not contact_by_name.is_known_address(sender_address)


def reader_failure_outcome(
    contact_by_name: Contact | None, is_impersonator: bool, reasons: tuple[ReasonCode, ...]
) -> PolicyOutcome:
    """A Reader failure is a gate-1 HOLD (single tap) when gate 1 fires, otherwise WARN unreadable; never PASS."""
    if is_impersonator:
        return PolicyOutcome(PolicyDecision.HOLD, None, reasons, contact_by_name, GateFired.GATE_1, Friction.SINGLE_TAP)
    return PolicyOutcome(PolicyDecision.WARN, WarnReason.UNREADABLE, reasons)


def outcome_for_reasons_alone(reasons: tuple[ReasonCode, ...]) -> PolicyOutcome:
    """No ask: reasons give WARN (link_reasons when every reason is a link reason, else listed_sender); none give PASS."""
    if reasons:
        is_links_only = all(reason in LINK_REASON_CODES for reason in reasons)
        return PolicyOutcome(PolicyDecision.WARN, WarnReason.LINK_REASONS if is_links_only else WarnReason.LISTED_SENDER, reasons)
    return PolicyOutcome(PolicyDecision.PASS, None, reasons)


def decide(
    reader_outcome: EmailVerdict | ReaderFailure,
    contact_by_name: Contact | None,
    contact_by_claim: Contact | None,
    sender_address: str,
    reasons: Sequence[ReasonCode],
) -> PolicyOutcome:
    """Gate 1 (code) holds a contact's name from an unknown address; gate 2 (model) holds a clear ask from a claimed contact.

    contact_by_name comes from the From display name only, so gate 1 never depends on the model.
    """
    reasons_list = list(reasons)
    is_impersonator = is_impersonator_of(contact_by_name, sender_address)
    if is_impersonator:
        reasons_list.append(ReasonCode.UNKNOWN_ADDRESS)
    if isinstance(reader_outcome, ReaderFailure):
        return reader_failure_outcome(contact_by_name, is_impersonator, tuple(reasons_list))
    verdict = reader_outcome
    has_ask = verdict.intent is not Intent.NONE
    is_clear_ask = has_ask and verdict.confidence >= HOLD_CONFIDENCE_THRESHOLD
    contact = contact_by_name or contact_by_claim
    is_address_known = contact is not None and contact.is_known_address(sender_address)
    if is_impersonator:
        friction = Friction.DOUBLE_TAP if verdict.intent in DOUBLE_TAP_INTENTS else Friction.SINGLE_TAP
        return PolicyOutcome(PolicyDecision.HOLD, None, tuple(reasons_list), contact_by_name, GateFired.GATE_1, friction)
    if is_clear_ask and contact is not None:
        return PolicyOutcome(
            PolicyDecision.HOLD, None, tuple(reasons_list), contact, GateFired.GATE_2, Friction.SINGLE_TAP, is_address_known
        )
    if has_ask and contact is None:
        return PolicyOutcome(PolicyDecision.WARN, WarnReason.UNKNOWN_NAME, tuple(reasons_list))
    if has_ask:
        return PolicyOutcome(PolicyDecision.WARN, WarnReason.LOW_CONFIDENCE, tuple(reasons_list))
    return outcome_for_reasons_alone(tuple(reasons_list))


def needs_escalation(outcome: PolicyOutcome, is_escalation_enabled: bool) -> bool:
    """Only an unknown-address hold is shown to the second reviewer; flag off → no call, the hold stands."""
    return is_escalation_enabled and outcome.decision is PolicyDecision.HOLD and not outcome.is_address_known


def is_cleared_by_reviewer(outcome: PolicyOutcome, answer: EscalationAnswer) -> bool:
    """Only a gate-2 hold the reviewer calls `warn` is cleared; gate 1 is code and never downgraded."""
    return outcome.gate is GateFired.GATE_2 and answer.decision is EscalationDecision.WARN


def apply_escalation(outcome: PolicyOutcome, answer: EscalationAnswer | EscalationFailure) -> PolicyOutcome:
    """A gate-2 hold the reviewer calls `warn` becomes WARN escalation_cleared; gate 1 is code and never downgraded."""
    if isinstance(answer, EscalationFailure):
        return replace(outcome, reasons=(*outcome.reasons, ReasonCode.ESCALATION_FAILED))
    if answer.contact_by_claim != outcome.contact:
        return replace(outcome, reasons=(*outcome.reasons, ReasonCode.PERSON_CONFLICT))
    if is_cleared_by_reviewer(outcome, answer):
        return PolicyOutcome(
            PolicyDecision.WARN, WarnReason.ESCALATION_CLEARED, (*outcome.reasons, ReasonCode.ESCALATION_CLEARED)
        )
    return outcome
