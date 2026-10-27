"""Each mutant breaks the production policy module one way; the golden rows must notice every one."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import pytest

import shared.policy as policy_module
from shared.enums import LINK_REASON_CODES, EscalationDecision, Friction, GateFired, Intent, PolicyDecision, ReasonCode
from shared.policy import PolicyOutcome, ReaderFailure
from tests.policy_golden import (
    ESCALATION_ROWS, FRIEND_ADDRESS, GATE_1_ROWS, GATE_2_ROWS, JIMIN, LINK_REASON_ROWS, NEWSLETTER_ADDRESS,
    READER_FAILURE_ROWS, STRANGER_ADDRESS, make_verdict,
)


class PolicyMutant(StrEnum):
    GATE_1_SKIPPED = "gate_1_skipped"
    READER_FAILURE_PASSES = "reader_failure_passes"
    THRESHOLD_ZERO = "threshold_zero"
    THRESHOLD_ABOVE_ONE = "threshold_above_one"
    DOUBLE_TAP_NEVER = "double_tap_never"
    LINK_REASONS_HOLD = "link_reasons_hold"
    GATE_1_DOWNGRADED = "gate_1_downgraded"


@dataclass(frozen=True)
class GoldenRowResult:
    row_label: str
    observed_outcome: PolicyOutcome
    expected_outcome: PolicyOutcome


def evaluate_gate_2_rows() -> list[GoldenRowResult]:
    results = []
    for row_index, (intent, confidence, contact_by_claim, reasons, expected_decision, expected_warn_reason) in enumerate(GATE_2_ROWS):
        observed_outcome = policy_module.decide(make_verdict(intent, confidence), None, contact_by_claim, FRIEND_ADDRESS, reasons)
        if expected_decision is PolicyDecision.HOLD:
            expected_outcome = PolicyOutcome(expected_decision, None, tuple(reasons), JIMIN, GateFired.GATE_2, Friction.SINGLE_TAP, False)
        else:
            expected_outcome = PolicyOutcome(expected_decision, expected_warn_reason, tuple(reasons))
        results.append(GoldenRowResult(f"GATE_2_ROWS[{row_index}]", observed_outcome, expected_outcome))
    return results


def evaluate_gate_1_rows() -> list[GoldenRowResult]:
    results = []
    for row_index, (intent, confidence, expected_friction) in enumerate(GATE_1_ROWS):
        observed_outcome = policy_module.decide(make_verdict(intent, confidence, claimed_person=None), JIMIN, None, STRANGER_ADDRESS, [])
        expected_outcome = PolicyOutcome(PolicyDecision.HOLD, None, (ReasonCode.UNKNOWN_ADDRESS,), JIMIN, GateFired.GATE_1, expected_friction, False)
        results.append(GoldenRowResult(f"GATE_1_ROWS[{row_index}]", observed_outcome, expected_outcome))
    return results


def evaluate_reader_failure_rows() -> list[GoldenRowResult]:
    return [
        GoldenRowResult(
            f"READER_FAILURE_ROWS[{row_index}]",
            policy_module.decide(ReaderFailure("schema invalid after retry"), contact_by_name, None, STRANGER_ADDRESS, []),
            expected_outcome,
        )
        for row_index, (contact_by_name, expected_outcome) in enumerate(READER_FAILURE_ROWS)
    ]


def evaluate_link_reason_rows() -> list[GoldenRowResult]:
    return [
        GoldenRowResult(
            f"LINK_REASON_ROWS[{row_index}]",
            policy_module.decide(make_verdict(Intent.NONE, 0.0, claimed_person=None), None, None, NEWSLETTER_ADDRESS, reasons),
            expected_outcome,
        )
        for row_index, (reasons, expected_outcome) in enumerate(LINK_REASON_ROWS)
    ]


def evaluate_escalation_rows() -> list[GoldenRowResult]:
    return [
        GoldenRowResult(f"ESCALATION_ROWS[{row_index}]", policy_module.apply_escalation(outcome_before, answer), expected_outcome)
        for row_index, (outcome_before, answer, expected_outcome) in enumerate(ESCALATION_ROWS)
    ]


def evaluate_every_golden_row() -> list[GoldenRowResult]:
    return [
        *evaluate_gate_2_rows(), *evaluate_gate_1_rows(), *evaluate_reader_failure_rows(),
        *evaluate_link_reason_rows(), *evaluate_escalation_rows(),
    ]


def apply_mutant(mutant: PolicyMutant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces one seam of the production policy module so the policy is wrong in exactly this mutant's way."""
    match mutant:
        case PolicyMutant.GATE_1_SKIPPED:
            monkeypatch.setattr(policy_module, "is_impersonator_of", lambda contact_by_name, sender_address: False)
        case PolicyMutant.READER_FAILURE_PASSES:
            monkeypatch.setattr(
                policy_module, "reader_failure_outcome",
                lambda contact_by_name, is_impersonator, reasons: PolicyOutcome(PolicyDecision.PASS, None, reasons),
            )
        case PolicyMutant.THRESHOLD_ZERO:
            monkeypatch.setattr(policy_module, "HOLD_CONFIDENCE_THRESHOLD", 0.0)
        case PolicyMutant.THRESHOLD_ABOVE_ONE:
            monkeypatch.setattr(policy_module, "HOLD_CONFIDENCE_THRESHOLD", 1.01)
        case PolicyMutant.DOUBLE_TAP_NEVER:
            monkeypatch.setattr(policy_module, "DOUBLE_TAP_INTENTS", ())
        case PolicyMutant.LINK_REASONS_HOLD:
            monkeypatch.setattr(policy_module, "outcome_for_reasons_alone", make_link_reasons_hold(policy_module.outcome_for_reasons_alone))
        case PolicyMutant.GATE_1_DOWNGRADED:
            monkeypatch.setattr(
                policy_module, "is_cleared_by_reviewer",
                lambda outcome, answer: answer.decision is EscalationDecision.WARN,
            )


def make_link_reasons_hold(
    production_outcome_for_reasons_alone: Callable[[tuple[ReasonCode, ...]], PolicyOutcome],
) -> Callable[[tuple[ReasonCode, ...]], PolicyOutcome]:
    """Link-only reasons HOLD instead of WARN; every other case keeps the production answer."""

    def outcome_with_link_reasons_held(reasons: tuple[ReasonCode, ...]) -> PolicyOutcome:
        if reasons and all(reason in LINK_REASON_CODES for reason in reasons):
            return PolicyOutcome(PolicyDecision.HOLD, None, reasons)
        return production_outcome_for_reasons_alone(reasons)

    return outcome_with_link_reasons_held


@pytest.mark.parametrize("mutant", list(PolicyMutant), ids=[str(mutant) for mutant in PolicyMutant])
def test_every_mutant_flips_at_least_one_golden_row(mutant: PolicyMutant, monkeypatch: pytest.MonkeyPatch):
    production_mismatches = [result.row_label for result in evaluate_every_golden_row() if result.observed_outcome != result.expected_outcome]
    assert production_mismatches == [], "the unmutated policy must match every golden row"
    apply_mutant(mutant, monkeypatch)
    flipped_rows = [result.row_label for result in evaluate_every_golden_row() if result.observed_outcome != result.expected_outcome]
    assert flipped_rows, f"no golden row notices the {mutant} mutant"
