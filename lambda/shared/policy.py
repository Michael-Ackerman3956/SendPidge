"""The hold / warn / pass decision. Pure code: no model, no network, no side effects."""

from collections.abc import Sequence
from dataclasses import dataclass

from shared.enums import Intent, PolicyDecision, ReasonCode, WarnReason
from shared.reader_agent import EmailVerdict

HOLD_CONFIDENCE_THRESHOLD = 0.6


@dataclass(frozen=True)
class ReaderFailure:
    """The Reader returned no valid verdict. Never released, never held."""

    error_message: str


@dataclass(frozen=True)
class PolicyOutcome:
    decision: PolicyDecision
    warn_reason: WarnReason | None
    reasons: tuple[ReasonCode, ...]


def decide(
    reader_outcome: EmailVerdict | ReaderFailure,
    is_claimed_person_a_contact: bool,
    reasons: Sequence[ReasonCode],
) -> PolicyOutcome:
    """HOLD only for a clear ask from a known name; everything else marks or passes, never silently."""
    reasons_tuple = tuple(reasons)
    if isinstance(reader_outcome, ReaderFailure):
        return PolicyOutcome(PolicyDecision.WARN, WarnReason.UNREADABLE, reasons_tuple)
    verdict = reader_outcome
    has_ask = verdict.intent is not Intent.NONE
    is_clear_ask = has_ask and verdict.confidence >= HOLD_CONFIDENCE_THRESHOLD
    if is_clear_ask and is_claimed_person_a_contact:
        return PolicyOutcome(PolicyDecision.HOLD, None, reasons_tuple)
    if has_ask and not is_claimed_person_a_contact:
        return PolicyOutcome(PolicyDecision.WARN, WarnReason.UNKNOWN_NAME, reasons_tuple)
    if has_ask:
        return PolicyOutcome(PolicyDecision.WARN, WarnReason.LOW_CONFIDENCE, reasons_tuple)
    if reasons_tuple:
        return PolicyOutcome(PolicyDecision.WARN, WarnReason.LISTED_SENDER, reasons_tuple)
    return PolicyOutcome(PolicyDecision.PASS, None, reasons_tuple)
