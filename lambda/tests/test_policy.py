"""The two gates and the second reviewer, as a table. Gate 1 is code and nothing downgrades it."""

import pytest

from shared import sanitize
from shared.auth import authentication_reasons
from shared.enums import EscalationDecision, Friction, GateFired, Intent, PolicyDecision, ReasonCode, WarnReason
from shared.escalation import EscalationVerdict, escalation_fields
from shared.graph import Contact
from shared.policy import (
    CLAIMED_PERSON_MAX_LENGTH, UNREADABLE_SUMMARY, EscalationAnswer, EscalationFailure, PolicyOutcome, ReaderFailure,
    apply_escalation, decide, needs_escalation, stored_claimed_person, verdict_fields_for_request,
)
from tests.policy_golden import (
    AUTHENTICATION_ROWS, GATE_1_ROWS, GATE_2_ROWS, JIMIN, JIMIN_ADDRESS, STRANGER_ADDRESS, make_verdict,
)


@pytest.mark.parametrize(
    ("intent", "confidence", "contact_by_claim", "reasons", "expected_decision", "expected_warn_reason"),
    GATE_2_ROWS,
)
def test_gate_2_table_with_no_display_name_match(intent, confidence, contact_by_claim, reasons, expected_decision, expected_warn_reason):
    """The From display name matches no contact, so only the model's claim can hold (claimed-jimin.eml shape)."""
    outcome = decide(make_verdict(intent, confidence), None, contact_by_claim, "minho.kim@friend-mail.example", reasons)
    assert (outcome.decision, outcome.warn_reason, outcome.reasons) == (expected_decision, expected_warn_reason, tuple(reasons))
    if expected_decision is PolicyDecision.HOLD:
        assert (outcome.contact, outcome.gate, outcome.friction, outcome.is_address_known) == (JIMIN, GateFired.GATE_2, Friction.SINGLE_TAP, False)


@pytest.mark.parametrize(
    ("intent", "confidence", "expected_friction"),
    GATE_1_ROWS,
)
def test_gate_1_holds_a_contact_name_from_an_unknown_address_regardless_of_the_verdict(intent, confidence, expected_friction):
    outcome = decide(make_verdict(intent, confidence, claimed_person=None), JIMIN, None, STRANGER_ADDRESS, [])
    assert outcome == PolicyOutcome(PolicyDecision.HOLD, None, (ReasonCode.UNKNOWN_ADDRESS,), JIMIN, GateFired.GATE_1, expected_friction, False)


def test_gate_1_does_not_fire_from_a_known_address_but_gate_2_still_can():
    hello = decide(make_verdict(Intent.NONE, 0.0), JIMIN, JIMIN, JIMIN_ADDRESS, [])
    assert hello == PolicyOutcome(PolicyDecision.PASS, None, ())
    real_ask = decide(make_verdict(Intent.MONEY, 0.9), JIMIN, JIMIN, JIMIN_ADDRESS.upper(), [])
    assert (real_ask.decision, real_ask.gate, real_ask.is_address_known) == (PolicyDecision.HOLD, GateFired.GATE_2, True)


def test_gate_1_fires_with_an_empty_known_address_list():
    unseeded = Contact("CONTACT#jimin", "Jimin", "jimin")
    outcome = decide(make_verdict(Intent.NONE, 0.0), unseeded, None, JIMIN_ADDRESS, [])
    assert (outcome.decision, outcome.gate) == (PolicyDecision.HOLD, GateFired.GATE_1)


def test_reader_failure_is_warn_never_pass_unless_gate_1_holds():
    warn = decide(ReaderFailure("schema invalid after retry"), None, None, STRANGER_ADDRESS, [])
    assert (warn.decision, warn.warn_reason) == (PolicyDecision.WARN, WarnReason.UNREADABLE)
    held = decide(ReaderFailure("schema invalid after retry"), JIMIN, None, STRANGER_ADDRESS, [])
    assert (held.decision, held.gate, held.friction, held.reasons) == (PolicyDecision.HOLD, GateFired.GATE_1, Friction.SINGLE_TAP, (ReasonCode.UNKNOWN_ADDRESS,))
    assert verdict_fields_for_request(ReaderFailure("x"), "")["summary"] == UNREADABLE_SUMMARY


def test_reasons_never_create_a_hold():
    outcome = decide(make_verdict(Intent.NONE, 0.9), None, JIMIN, STRANGER_ADDRESS, [ReasonCode.AUTH_FAILED, ReasonCode.AUTH_MISSING])
    assert outcome.decision is PolicyDecision.WARN
    assert outcome.reasons == (ReasonCode.AUTH_FAILED, ReasonCode.AUTH_MISSING)


def gate_1_hold() -> PolicyOutcome:
    return decide(make_verdict(Intent.MONEY, 0.9), JIMIN, JIMIN, STRANGER_ADDRESS, [])


def gate_2_hold() -> PolicyOutcome:
    return decide(make_verdict(Intent.MONEY, 0.9), None, JIMIN, "minho.kim@friend-mail.example", [])


def test_escalation_runs_only_for_unknown_address_holds_with_the_flag_on():
    assert needs_escalation(gate_1_hold(), True)
    assert needs_escalation(gate_2_hold(), True)
    assert not needs_escalation(gate_1_hold(), False)
    known_address_hold = decide(make_verdict(Intent.MONEY, 0.9), JIMIN, JIMIN, JIMIN_ADDRESS, [])
    assert not needs_escalation(known_address_hold, True)
    assert not needs_escalation(decide(make_verdict(Intent.NONE, 0.0), None, None, STRANGER_ADDRESS, []), True)


def test_gate_2_hold_cleared_by_the_reviewer_becomes_a_warn():
    outcome = apply_escalation(gate_2_hold(), EscalationAnswer(EscalationDecision.WARN, JIMIN))
    assert outcome == PolicyOutcome(PolicyDecision.WARN, WarnReason.ESCALATION_CLEARED, (ReasonCode.ESCALATION_CLEARED,))


def test_gate_1_hold_is_never_downgraded_by_the_reviewer():
    assert apply_escalation(gate_1_hold(), EscalationAnswer(EscalationDecision.WARN, JIMIN)) == gate_1_hold()


def test_reviewer_saying_hold_changes_nothing():
    assert apply_escalation(gate_2_hold(), EscalationAnswer(EscalationDecision.HOLD, JIMIN)) == gate_2_hold()


def test_reviewer_naming_another_person_keeps_the_hold_with_person_conflict():
    outcome = apply_escalation(gate_2_hold(), EscalationAnswer(EscalationDecision.WARN, None))
    assert (outcome.decision, outcome.reasons) == (PolicyDecision.HOLD, (ReasonCode.PERSON_CONFLICT,))


def test_failed_reviewer_call_keeps_the_hold_with_escalation_failed():
    outcome = apply_escalation(gate_2_hold(), EscalationFailure("AccessDeniedException"))
    assert (outcome.decision, outcome.reasons) == (PolicyDecision.HOLD, (ReasonCode.ESCALATION_FAILED,))


def test_confidence_must_be_between_zero_and_one():
    with pytest.raises(ValueError):
        make_verdict(Intent.MONEY, 1.2)


@pytest.mark.parametrize(
    ("header_values", "expected_reasons"),
    AUTHENTICATION_ROWS,
)
def test_authentication_reasons(header_values, expected_reasons):
    assert authentication_reasons(header_values) == expected_reasons


def test_link_reasons_alone_warn_never_hold():
    outcome = decide(make_verdict(Intent.NONE, 0.0, claimed_person=None), None, None, "hello@gardenweekly.example", [ReasonCode.LINK_TEXT_HREF_MISMATCH])
    assert (outcome.decision, outcome.warn_reason, outcome.reasons) == (PolicyDecision.WARN, WarnReason.LINK_REASONS, (ReasonCode.LINK_TEXT_HREF_MISMATCH,))
    assert outcome.contact is None and outcome.gate is None


def test_camouflage_reasons_alone_warn_never_hold():
    camouflage_reasons = [
        ReasonCode.WHOLE_BODY_LINK, ReasonCode.SCRIPT_HREF_LINK, ReasonCode.CSS_HIDDEN_LINK, ReasonCode.IMAGE_MAP_LINK,
        ReasonCode.HTML_ATTACHMENT_LINK, ReasonCode.LINKS_TRUNCATED, ReasonCode.IDN_LINK_DOMAIN, ReasonCode.IMAGE_ONLY_BODY,
    ]
    for reason in camouflage_reasons:
        outcome = decide(make_verdict(Intent.NONE, 0.0, claimed_person=None), None, None, "hello@gardenweekly.example", [reason])
        assert (outcome.decision, outcome.warn_reason, outcome.reasons) == (PolicyDecision.WARN, WarnReason.LINK_REASONS, (reason,)), reason
        assert (outcome.gate, outcome.friction, outcome.contact) == (None, None, None), reason
    every_reason = decide(make_verdict(Intent.NONE, 0.0, claimed_person=None), None, None, "a@b.example", camouflage_reasons)
    assert (every_reason.decision, every_reason.warn_reason) == (PolicyDecision.WARN, WarnReason.LINK_REASONS)


def test_link_only_reasons_pick_warn_reason_link_reasons():
    links_only = decide(make_verdict(Intent.NONE, 0.0, claimed_person=None), None, None, "a@b.example", [ReasonCode.HIDDEN_LINK, ReasonCode.KNOWN_SHORTENER])
    mixed = decide(make_verdict(Intent.NONE, 0.0, claimed_person=None), None, None, "a@b.example", [ReasonCode.AUTH_FAILED, ReasonCode.HIDDEN_LINK])
    held = decide(make_verdict(Intent.MONEY, 0.9), None, JIMIN, "a@b.example", [ReasonCode.LINK_TEXT_HREF_MISMATCH])
    assert links_only.warn_reason is WarnReason.LINK_REASONS
    assert mixed.warn_reason is WarnReason.LISTED_SENDER
    assert (held.decision, held.reasons) == (PolicyDecision.HOLD, (ReasonCode.LINK_TEXT_HREF_MISMATCH,))


def test_claimed_person_is_sanitised_and_capped_when_stored():
    raw_claim = "Jimin call 010-1234-5678 at bank-help.example " + "x" * 200
    stored = verdict_fields_for_request(make_verdict(Intent.MONEY, 0.9, claimed_person=raw_claim), "summary")["claimed_person"]
    assert not any(character.isdigit() for character in stored) and "bank-help" not in stored
    assert stored.startswith("Jimin call") and len(stored) <= CLAIMED_PERSON_MAX_LENGTH == 60
    assert verdict_fields_for_request(make_verdict(Intent.MONEY, 0.9, claimed_person=None), "summary")["claimed_person"] is None


def test_a_claimed_name_that_falls_back_is_stored_as_no_name(monkeypatch):
    monkeypatch.setattr(sanitize, "strip_links_and_addresses", lambda text: text)  # a span survives the strip
    raw_claim = "Jimin at evil.com"
    assert sanitize.sanitize_summary(raw_claim) == sanitize.SUMMARY_FALLBACK_TEXT
    assert stored_claimed_person(raw_claim) is None


def test_the_second_reviewers_claimed_name_is_stored_like_the_readers():
    raw_claim = "Jimin call 010-1234-5678 at bank-help.example " + "x" * 200
    reviewer_verdict = EscalationVerdict(claimed_person=raw_claim, intent=Intent.MONEY, confidence=0.9,
                                         decision=EscalationDecision.HOLD, rationale="asks for money")
    stored = escalation_fields(reviewer_verdict)["claimed_person"]
    assert not any(character.isdigit() for character in stored) and "bank-help" not in stored
    assert stored.startswith("Jimin call") and len(stored) <= CLAIMED_PERSON_MAX_LENGTH
