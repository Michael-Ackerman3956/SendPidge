import pytest

from shared.auth import authentication_reasons
from shared.enums import Intent, PolicyDecision, ReasonCode, WarnReason
from shared.policy import HOLD_CONFIDENCE_THRESHOLD, PolicyOutcome, ReaderFailure, decide
from shared.reader_agent import EmailVerdict


def make_verdict(intent: Intent, confidence: float, claimed_person: str | None = "Jimin") -> EmailVerdict:
    return EmailVerdict(claimed_person=claimed_person, intent=intent, confidence=confidence, summary="Someone asks for something.")


@pytest.mark.parametrize(
    ("intent", "confidence", "is_contact", "reasons", "expected_decision", "expected_warn_reason"),
    [
        (Intent.MONEY, 0.9, True, [], PolicyDecision.HOLD, None),
        (Intent.CODE, HOLD_CONFIDENCE_THRESHOLD, True, [], PolicyDecision.HOLD, None),
        (Intent.URGENT_ACTION, 0.7, True, [ReasonCode.DMARC_FAIL], PolicyDecision.HOLD, None),
        (Intent.MONEY, 0.9, False, [], PolicyDecision.WARN, WarnReason.UNKNOWN_NAME),
        (Intent.MONEY, 0.3, False, [], PolicyDecision.WARN, WarnReason.UNKNOWN_NAME),
        (Intent.MONEY, 0.3, True, [], PolicyDecision.WARN, WarnReason.LOW_CONFIDENCE),
        (Intent.NONE, 0.9, True, [ReasonCode.AUTH_TAMPERED], PolicyDecision.WARN, WarnReason.LISTED_SENDER),
        (Intent.NONE, 0.9, True, [], PolicyDecision.PASS, None),
        (Intent.NONE, 0.0, False, [], PolicyDecision.PASS, None),
    ],
)
def test_decision_table(intent, confidence, is_contact, reasons, expected_decision, expected_warn_reason):
    outcome = decide(make_verdict(intent, confidence), is_contact, reasons)
    assert outcome == PolicyOutcome(expected_decision, expected_warn_reason, tuple(reasons))


def test_reader_failure_is_warn_never_pass_never_hold():
    outcome = decide(ReaderFailure("schema invalid after retry"), True, [])
    assert outcome.decision is PolicyDecision.WARN
    assert outcome.warn_reason is WarnReason.UNREADABLE


def test_reasons_never_create_a_hold():
    outcome = decide(make_verdict(Intent.NONE, 0.9), True, [ReasonCode.DMARC_FAIL, ReasonCode.AUTH_MISSING])
    assert outcome.decision is PolicyDecision.WARN
    assert outcome.reasons == (ReasonCode.DMARC_FAIL, ReasonCode.AUTH_MISSING)


def test_confidence_must_be_between_zero_and_one():
    with pytest.raises(ValueError):
        make_verdict(Intent.MONEY, 1.2)


@pytest.mark.parametrize(
    ("header_values", "expected_reasons"),
    [
        ([], [ReasonCode.AUTH_MISSING]),
        (["spf=pass;dkim=pass;dmarc=pass"], []),
        (["spf=pass; dkim=pass; dmarc=FAIL"], [ReasonCode.DMARC_FAIL]),
        (["spf=pass;dkim=pass;dmarc=pass", "spf=pass;dkim=pass;dmarc=pass"], [ReasonCode.AUTH_TAMPERED]),
        (["spf=pass;dkim=pass;dmarc=fail", "spf=fail"], [ReasonCode.AUTH_TAMPERED]),
    ],
)
def test_authentication_reasons(header_values, expected_reasons):
    assert authentication_reasons(header_values) == expected_reasons
