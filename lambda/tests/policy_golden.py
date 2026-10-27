"""Golden rows for the policy: fixed inputs and the outcomes production returns for them.

test_policy.py runs the gate and authentication tables; test_policy_mutants.py runs every table under each mutant.
"""

from shared.enums import EscalationDecision, Friction, GateFired, Intent, PolicyDecision, ReasonCode, WarnReason
from shared.graph import Contact
from shared.policy import HOLD_CONFIDENCE_THRESHOLD, EscalationAnswer, PolicyOutcome
from shared.reader_agent import EmailVerdict

JIMIN = Contact("CONTACT#jimin", "Jimin", "jimin", known_addresses=("jimin@example.com",))
JIMIN_ADDRESS = "jimin@example.com"
STRANGER_ADDRESS = "jimin.kim@jimin-help.example"
FRIEND_ADDRESS = "minho.kim@friend-mail.example"  # matches no contact's display name (claimed-jimin.eml shape)
NEWSLETTER_ADDRESS = "hello@gardenweekly.example"


def make_verdict(intent: Intent, confidence: float, claimed_person: str | None = "Jimin") -> EmailVerdict:
    return EmailVerdict(claimed_person=claimed_person, intent=intent, confidence=confidence, summary="Someone asks for something.")


# (intent, confidence, contact_by_claim, reasons, expected_decision, expected_warn_reason); From is FRIEND_ADDRESS, no display-name match.
GATE_2_ROWS = [
    (Intent.MONEY, 0.9, JIMIN, [], PolicyDecision.HOLD, None),
    (Intent.CODE, HOLD_CONFIDENCE_THRESHOLD, JIMIN, [], PolicyDecision.HOLD, None),
    (Intent.URGENT_ACTION, 0.7, JIMIN, [ReasonCode.AUTH_FAILED], PolicyDecision.HOLD, None),
    (Intent.MONEY, 0.9, None, [], PolicyDecision.WARN, WarnReason.UNKNOWN_NAME),
    (Intent.MONEY, 0.3, None, [], PolicyDecision.WARN, WarnReason.UNKNOWN_NAME),
    (Intent.MONEY, 0.3, JIMIN, [], PolicyDecision.WARN, WarnReason.LOW_CONFIDENCE),
    (Intent.NONE, 0.9, JIMIN, [ReasonCode.AUTH_TAMPERED], PolicyDecision.WARN, WarnReason.LISTED_SENDER),
    (Intent.NONE, 0.9, JIMIN, [], PolicyDecision.PASS, None),
    (Intent.NONE, 0.0, None, [], PolicyDecision.PASS, None),
]

# (intent, confidence, expected_friction); display name JIMIN from STRANGER_ADDRESS, no claim, no reasons.
GATE_1_ROWS = [
    (Intent.MONEY, 0.95, Friction.DOUBLE_TAP),
    (Intent.CODE, 0.2, Friction.DOUBLE_TAP),
    (Intent.URGENT_ACTION, 0.9, Friction.SINGLE_TAP),
    (Intent.NONE, 0.0, Friction.SINGLE_TAP),
]

# (authentication header values, expected reasons)
AUTHENTICATION_ROWS = [
    ([], [ReasonCode.AUTH_MISSING]),
    (["spf=pass;dkim=pass"], []),
    (["spf=fail;dkim=pass"], []),
    (["spf=pass; dkim=FAIL"], []),
    (["spf=fail;dkim=fail"], [ReasonCode.AUTH_FAILED]),
    (["spf=pass;dkim=pass", "spf=pass;dkim=pass"], [ReasonCode.AUTH_TAMPERED]),
    (["spf=fail;dkim=fail", "spf=pass;dkim=pass"], [ReasonCode.AUTH_TAMPERED]),
]

# (contact_by_name, expected outcome); the Reader failed, From is STRANGER_ADDRESS, no claim, no reasons.
READER_FAILURE_ROWS = [
    (None, PolicyOutcome(PolicyDecision.WARN, WarnReason.UNREADABLE, ())),
    (JIMIN, PolicyOutcome(PolicyDecision.HOLD, None, (ReasonCode.UNKNOWN_ADDRESS,), JIMIN, GateFired.GATE_1, Friction.SINGLE_TAP)),
]

# (reasons, expected outcome); no ask, no claim, no display-name match, From is NEWSLETTER_ADDRESS.
LINK_REASON_ROWS = [
    ([ReasonCode.LINK_TEXT_HREF_MISMATCH], PolicyOutcome(PolicyDecision.WARN, WarnReason.LINK_REASONS, (ReasonCode.LINK_TEXT_HREF_MISMATCH,))),
    (
        [ReasonCode.HIDDEN_LINK, ReasonCode.KNOWN_SHORTENER],
        PolicyOutcome(PolicyDecision.WARN, WarnReason.LINK_REASONS, (ReasonCode.HIDDEN_LINK, ReasonCode.KNOWN_SHORTENER)),
    ),
    (
        [ReasonCode.AUTH_FAILED, ReasonCode.HIDDEN_LINK],
        PolicyOutcome(PolicyDecision.WARN, WarnReason.LISTED_SENDER, (ReasonCode.AUTH_FAILED, ReasonCode.HIDDEN_LINK)),
    ),
]

GATE_1_MONEY_HOLD = PolicyOutcome(PolicyDecision.HOLD, None, (ReasonCode.UNKNOWN_ADDRESS,), JIMIN, GateFired.GATE_1, Friction.DOUBLE_TAP)
GATE_2_MONEY_HOLD = PolicyOutcome(PolicyDecision.HOLD, None, (), JIMIN, GateFired.GATE_2, Friction.SINGLE_TAP)

# (outcome before the reviewer, reviewer answer, expected outcome after apply_escalation)
ESCALATION_ROWS = [
    (GATE_1_MONEY_HOLD, EscalationAnswer(EscalationDecision.WARN, JIMIN), GATE_1_MONEY_HOLD),
    (
        GATE_2_MONEY_HOLD,
        EscalationAnswer(EscalationDecision.WARN, JIMIN),
        PolicyOutcome(PolicyDecision.WARN, WarnReason.ESCALATION_CLEARED, (ReasonCode.ESCALATION_CLEARED,)),
    ),
    (GATE_2_MONEY_HOLD, EscalationAnswer(EscalationDecision.HOLD, JIMIN), GATE_2_MONEY_HOLD),
]
