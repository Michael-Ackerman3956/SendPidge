"""The second reviewer: a raised call is a failure (hold stays), facts never carry the Reader's summary, the stored shape."""

from shared.email_parse import ParsedEmail
from shared.enums import EscalationDecision, GateFired, Intent, ReasonCode
from shared.escalation import EscalationFacts, EscalationVerdict, escalate, escalation_fields, format_facts
from shared.policy import EscalationFailure
from shared.reader_agent import EmailVerdict

EMAIL = ParsedEmail("Jimin", "jimin.kim@jimin-help.example", "jimin-help.example", "Urgent", "Send money today.", ("spf=pass;dkim=pass",))
VERDICT = EmailVerdict(claimed_person="Jimin", intent=Intent.MONEY, confidence=0.9, summary="SECRET SUMMARY asks for money")
FACTS = EscalationFacts(GateFired.GATE_2, False, False, "spf=pass;dkim=pass", (ReasonCode.AUTH_FAILED,), VERDICT)


class RaisingReviewer:
    def __call__(self, prompt, structured_output_model):
        raise RuntimeError("AccessDeniedException")


class AnsweringReviewer:
    def __init__(self, verdict):
        self.verdict = verdict
        self.prompts = []

    def __call__(self, prompt, structured_output_model):
        self.prompts.append(prompt)
        assert structured_output_model is EscalationVerdict
        return type("Result", (), {"structured_output": self.verdict})()


def test_a_raised_call_is_a_failure_not_an_exception():
    assert escalate(RaisingReviewer(), EMAIL, FACTS) == EscalationFailure("AccessDeniedException")


def test_the_prompt_carries_the_facts_and_the_body_but_never_the_reader_summary():
    reviewer = AnsweringReviewer(EscalationVerdict(claimed_person="Jimin", intent=Intent.MONEY, confidence=0.9,
                                                   decision=EscalationDecision.HOLD, rationale="asks for money"))
    verdict = escalate(reviewer, EMAIL, FACTS)
    assert verdict.decision is EscalationDecision.HOLD
    prompt = reviewer.prompts[0]
    assert "gate_fired: gate_2" in prompt and "auth_failed" in prompt and "Send money today." in prompt
    assert "SECRET SUMMARY" not in prompt and "SECRET SUMMARY" not in format_facts(FACTS)


def test_stored_fields_drop_the_rationale_and_mark_a_failure():
    verdict = EscalationVerdict(claimed_person="Jimin", intent=Intent.MONEY, confidence=0.9,
                                decision=EscalationDecision.WARN, rationale="logged only")
    assert escalation_fields(verdict) == {"claimed_person": "Jimin", "intent": "money", "confidence": 0.9, "decision": "warn", "failed": False}
    assert escalation_fields(EscalationFailure("throttled")) == {"decision": "hold", "failed": True}
