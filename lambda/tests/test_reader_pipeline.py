"""The five samples through the production pidge_reader handler and the real Strands loop, with a scripted model in place of
Bedrock: parse → links → lists → Reader → policy → EMAIL#/REQUEST# rows → the pushed email_state."""

import importlib
from dataclasses import dataclass
from pathlib import Path

from boto3.dynamodb.conditions import Attr

from shared import graph
from shared.enums import Channel, EscalationDecision, Friction, GateFired, Intent, PolicyDecision, ReasonCode, WarnReason
from shared.escalation import EscalationVerdict
from shared.reader_agent import EmailVerdict
from tests.helpers.scripted_model import ScriptedModel, ScriptedStep, text_reply_step, verdict_step
from tests.test_reader_reading_state import READER_RESIDENT_ADDRESS, inbound_event, seed_grandma_and_jimin_for_the_reader
from tests.test_verdict import aws  # noqa: F401

SAMPLES_DIRECTORY = Path(__file__).resolve().parent.parent.parent / "samples"

JIMIN_MONEY_ASK = {"claimed_person": "Jimin", "intent": Intent.MONEY, "confidence": 0.95,
                   "summary": "Asks for money for a hospital deposit."}
CLAIMED_JIMIN_MONEY_ASK = {"claimed_person": "Jimin", "intent": Intent.MONEY, "confidence": 0.9,
                           "summary": "Asks for money for an exam fee."}
NEWSLETTER_NO_ASK = {"claimed_person": "Garden Weekly", "intent": Intent.NONE, "confidence": 0.9,
                     "summary": "A gardening newsletter for October."}
FAMILY_HELLO_NO_ASK = {"claimed_person": "Jimin", "intent": Intent.NONE, "confidence": 0.9,
                       "summary": "A thank-you note about the weekend."}
REVIEWER_SAYS_WARN_FOR_JIMIN = {"claimed_person": "Jimin", "intent": Intent.MONEY, "confidence": 0.9,
                                "decision": EscalationDecision.WARN, "rationale": "Reads like a real exam-fee ask."}
NOT_A_VERDICT_REPLY = "I think this email is probably fine, nothing to worry about."


@dataclass(frozen=True)
class SampleGolden:
    sample_file_name: str
    subject_fragment: str  # the script key: each sample's subject is unique among the five
    reader_verdict_fields: dict
    decision: PolicyDecision
    warn_reason: WarnReason | None
    reasons: tuple[ReasonCode, ...]
    gate: GateFired | None
    friction: Friction | None  # None: no REQUEST# row
    first_link_flagged: bool | None  # None: the sample has no links


FAKE_JIMIN = SampleGolden("fake-jimin.eml", "Grandma please help, urgent", JIMIN_MONEY_ASK, PolicyDecision.HOLD, None,
                          (ReasonCode.UNKNOWN_ADDRESS,), GateFired.GATE_1, Friction.DOUBLE_TAP, None)
CLAIMED_JIMIN = SampleGolden("claimed-jimin.eml", "writing from Minho's email", CLAIMED_JIMIN_MONEY_ASK, PolicyDecision.HOLD, None,
                             (), GateFired.GATE_2, Friction.SINGLE_TAP, None)
FAKE_JIMIN_LINK = SampleGolden("fake-jimin-link.eml", "Grandma please help, the hospital link", JIMIN_MONEY_ASK, PolicyDecision.HOLD,
                               None, (ReasonCode.LINK_TEXT_HREF_MISMATCH, ReasonCode.UNKNOWN_ADDRESS), GateFired.GATE_1,
                               Friction.DOUBLE_TAP, True)
NEWSLETTER_LINK = SampleGolden("newsletter-link.eml", "October in the garden", NEWSLETTER_NO_ASK, PolicyDecision.WARN,
                               WarnReason.LINK_REASONS, (ReasonCode.LINK_TEXT_HREF_MISMATCH,), None, None, True)
FAMILY_HELLO = SampleGolden("family-hello.eml", "Photos from the weekend", FAMILY_HELLO_NO_ASK, PolicyDecision.PASS, None,
                            (), None, None, None)
FIVE_SAMPLE_GOLDENS = (FAKE_JIMIN, CLAIMED_JIMIN, FAKE_JIMIN_LINK, NEWSLETTER_LINK, FAMILY_HELLO)


@dataclass
class LoadedReaderHandler:
    handler: object
    pushed_emails: list[dict]
    decided_gates: list[GateFired | None]  # one per email, from the production decide()


def load_reader_handler(monkeypatch, is_escalation_enabled: bool) -> LoadedReaderHandler:
    """pidge_reader.handler re-imported under the test environment; pushes and asks recorded, the table is moto's."""
    environment = {"EMAIL_BUCKET": "bucket", "READER_MODEL": "haiku-4.5", "ESCALATION_MODEL": "opus-4.5", "ASKS_PER_CONTACT_PER_HOUR": "3",
                   "ESCALATION_ENABLED": "true" if is_escalation_enabled else "false", "RETRY_SCHEDULER_ENABLED": "false"}
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    handler = importlib.import_module("pidge_reader.handler")
    importlib.reload(handler)
    loaded = LoadedReaderHandler(handler, [], [])
    def record_push(user_id, message):
        loaded.pushed_emails.append(message["email"])
        return 1
    monkeypatch.setattr(handler.channels, "push_to_user", record_push)
    async def asked_in_app(*_):
        return [Channel.INAPP]
    monkeypatch.setattr(handler.channels, "ask_on_every_channel", asked_in_app)
    production_decide = handler.decide
    def decide_and_record_gate(*arguments):
        outcome = production_decide(*arguments)
        loaded.decided_gates.append(outcome.gate)
        return outcome
    monkeypatch.setattr(handler, "decide", decide_and_record_gate)
    return loaded


def run_sample(loaded: LoadedReaderHandler, monkeypatch, sample_file_name: str) -> str:
    raw_email = (SAMPLES_DIRECTORY / sample_file_name).read_bytes()
    object_key = f"inbox/{sample_file_name}"
    def get_sample_object(Bucket, Key):
        assert (Bucket, Key) == ("bucket", object_key)
        return {"Body": type("Body", (), {"read": lambda self: raw_email})()}
    monkeypatch.setattr(loaded.handler.s3_client, "get_object", get_sample_object)
    loaded.handler.handle(inbound_event(sample_file_name, [READER_RESIDENT_ADDRESS]), None)
    return object_key


def request_rows() -> list[dict]:
    return graph.get_table().scan(FilterExpression=Attr("PK").begins_with("REQUEST#"))["Items"]


def reader_scripts_for(goldens: tuple[SampleGolden, ...]) -> dict[str, list[ScriptedStep]]:
    return {golden.subject_fragment: [verdict_step(golden.reader_verdict_fields)] for golden in goldens}


def assert_sample_reached_its_golden(loaded: LoadedReaderHandler, object_key: str, golden: SampleGolden) -> None:
    email_row = graph.get_email(object_key)
    assert email_row["state"] == str(golden.decision), golden.sample_file_name
    assert email_row["warnReason"] == (str(golden.warn_reason) if golden.warn_reason else None), golden.sample_file_name
    assert email_row["reasons"] == [str(reason) for reason in golden.reasons], golden.sample_file_name
    assert loaded.decided_gates[-1] == golden.gate, golden.sample_file_name
    if golden.first_link_flagged is None:
        assert email_row["links"] == [], golden.sample_file_name
    else:
        assert email_row["links"][0]["flagged"] is golden.first_link_flagged, golden.sample_file_name
    pushed_state = loaded.pushed_emails[-1]
    assert (pushed_state["emailId"], pushed_state["state"], pushed_state["warnReason"]) == (
        object_key, golden.decision, golden.warn_reason), golden.sample_file_name
    assert pushed_state["reasons"] == list(golden.reasons), golden.sample_file_name
    assert pushed_state["links"] == email_row["links"], golden.sample_file_name
    if golden.friction is None:
        assert email_row["requestId"] is None and pushed_state["request"] is None, golden.sample_file_name
        return
    request_row = graph.get_request(email_row["requestId"])
    assert (request_row["emailId"], request_row["friction"], request_row["claimedPerson"]) == (
        object_key, str(golden.friction), golden.reader_verdict_fields["claimed_person"]), golden.sample_file_name
    assert request_row["verdict"]["intent"] == str(golden.reader_verdict_fields["intent"]), golden.sample_file_name
    assert (pushed_state["request"]["requestId"], pushed_state["request"]["contactName"]) == (
        email_row["requestId"], "Jimin"), golden.sample_file_name


def test_each_sample_reaches_its_golden_rows_through_the_real_loop(aws, monkeypatch):  # noqa: F811
    seed_grandma_and_jimin_for_the_reader()
    loaded = load_reader_handler(monkeypatch, is_escalation_enabled=False)
    scripted_reader_model = ScriptedModel(EmailVerdict, reader_scripts_for(FIVE_SAMPLE_GOLDENS))
    loaded.handler.reader.model = scripted_reader_model
    for golden in FIVE_SAMPLE_GOLDENS:
        object_key = run_sample(loaded, monkeypatch, golden.sample_file_name)
        assert scripted_reader_model.raised_errors == [], golden.sample_file_name
        assert scripted_reader_model.recorded_calls[-1].matched_prompt_fragment == golden.subject_fragment
        assert_sample_reached_its_golden(loaded, object_key, golden)
    assert len(scripted_reader_model.recorded_calls) == len(FIVE_SAMPLE_GOLDENS)  # one model call per email, no retry
    assert [call.user_text_count for call in scripted_reader_model.recorded_calls] == [1] * len(FIVE_SAMPLE_GOLDENS)  # read alone
    assert sorted(row["emailId"] for row in request_rows()) == sorted(
        f"inbox/{golden.sample_file_name}" for golden in FIVE_SAMPLE_GOLDENS if golden.friction is not None)
    assert [email["state"] for email in loaded.pushed_emails] == [
        state for golden in FIVE_SAMPLE_GOLDENS for state in (None, golden.decision)]  # reading, then the verdict


def test_text_instead_of_the_verdict_tool_twice_is_warn_unreadable_never_pass(aws, monkeypatch):  # noqa: F811
    seed_grandma_and_jimin_for_the_reader()
    loaded = load_reader_handler(monkeypatch, is_escalation_enabled=False)
    scripted_reader_model = ScriptedModel(EmailVerdict, {
        FAMILY_HELLO.subject_fragment: [text_reply_step(NOT_A_VERDICT_REPLY), text_reply_step(NOT_A_VERDICT_REPLY)],
    })
    loaded.handler.reader.model = scripted_reader_model
    object_key = run_sample(loaded, monkeypatch, FAMILY_HELLO.sample_file_name)  # would PASS with a verdict
    assert [call.is_forced_retry for call in scripted_reader_model.recorded_calls] == [False, True]  # Strands forced one retry
    assert scripted_reader_model.recorded_calls[1].offered_tool_names == (scripted_reader_model.structured_output_tool_name,)
    assert scripted_reader_model.raised_errors == []
    email_row = graph.get_email(object_key)
    assert (email_row["state"], email_row["warnReason"], email_row["requestId"]) == (
        str(PolicyDecision.WARN), str(WarnReason.UNREADABLE), None)
    assert (loaded.pushed_emails[-1]["state"], loaded.pushed_emails[-1]["warnReason"]) == (PolicyDecision.WARN, WarnReason.UNREADABLE)
    assert request_rows() == []


def test_reader_and_reviewer_have_no_tools(aws, monkeypatch):  # noqa: F811
    seed_grandma_and_jimin_for_the_reader()
    loaded = load_reader_handler(monkeypatch, is_escalation_enabled=True)
    reader, reviewer = loaded.handler.reader, loaded.handler.escalation_reader
    assert (reader.tool_names, reviewer.tool_names) == ([], [])
    scripted_reader_model = ScriptedModel(EmailVerdict, reader_scripts_for((CLAIMED_JIMIN,)))
    scripted_reviewer_model = ScriptedModel(EscalationVerdict, {CLAIMED_JIMIN.subject_fragment: [verdict_step(REVIEWER_SAYS_WARN_FOR_JIMIN)]})
    reader.model, reviewer.model = scripted_reader_model, scripted_reviewer_model
    run_sample(loaded, monkeypatch, CLAIMED_JIMIN.sample_file_name)  # a gate-2 hold: both agents run
    for scripted_model in (scripted_reader_model, scripted_reviewer_model):
        assert [call.offered_tool_names for call in scripted_model.recorded_calls] == [(scripted_model.structured_output_tool_name,)]
    assert (reader.tool_names, reviewer.tool_names) == ([], [])  # the per-call output tool is removed again


def test_escalation_warn_on_claimed_jimin_clears_to_warn_but_never_gate_1(aws, monkeypatch):  # noqa: F811
    seed_grandma_and_jimin_for_the_reader()
    loaded = load_reader_handler(monkeypatch, is_escalation_enabled=True)
    loaded.handler.reader.model = ScriptedModel(EmailVerdict, reader_scripts_for((CLAIMED_JIMIN, FAKE_JIMIN)))
    scripted_reviewer_model = ScriptedModel(EscalationVerdict, {
        CLAIMED_JIMIN.subject_fragment: [verdict_step(REVIEWER_SAYS_WARN_FOR_JIMIN)],
        FAKE_JIMIN.subject_fragment: [verdict_step(REVIEWER_SAYS_WARN_FOR_JIMIN)],
    })
    loaded.handler.escalation_reader.model = scripted_reviewer_model

    cleared_key = run_sample(loaded, monkeypatch, CLAIMED_JIMIN.sample_file_name)
    cleared_row = graph.get_email(cleared_key)
    assert (cleared_row["state"], cleared_row["warnReason"], cleared_row["reasons"], cleared_row["requestId"]) == (
        str(PolicyDecision.WARN), str(WarnReason.ESCALATION_CLEARED), [str(ReasonCode.ESCALATION_CLEARED)], None)
    assert (cleared_row["escalation"]["decision"], cleared_row["escalation"]["failed"]) == (str(EscalationDecision.WARN), False)
    assert (loaded.pushed_emails[-1]["state"], loaded.pushed_emails[-1]["warnReason"]) == (
        PolicyDecision.WARN, WarnReason.ESCALATION_CLEARED)
    assert request_rows() == []

    held_key = run_sample(loaded, monkeypatch, FAKE_JIMIN.sample_file_name)  # same reviewer answer on a gate-1 hold
    held_row = graph.get_email(held_key)
    assert (held_row["state"], held_row["reasons"]) == (str(PolicyDecision.HOLD), [str(ReasonCode.UNKNOWN_ADDRESS)])
    assert loaded.decided_gates == [GateFired.GATE_2, GateFired.GATE_1]
    request_row = graph.get_request(held_row["requestId"])
    assert (request_row["friction"], request_row["escalation"]["decision"]) == (str(Friction.DOUBLE_TAP), str(EscalationDecision.WARN))
    assert loaded.pushed_emails[-1]["state"] == PolicyDecision.HOLD
    assert len(scripted_reviewer_model.recorded_calls) == 2 and scripted_reviewer_model.raised_errors == []
    assert [call.user_text_count for call in scripted_reviewer_model.recorded_calls] == [1, 1]  # the second never sees the first
