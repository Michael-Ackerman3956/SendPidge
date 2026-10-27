"""The founder-run agent eval, offline: the real case and chat agents (and the real judge agent) built by the production
builders, with ScriptedModel swapped in for Bedrock, on families seeded through the production writers."""

import json
import os
import re
import sys
from pathlib import Path

import moto.backend_index
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from shared import case_agent, chat_agent, graph  # noqa: E402
from shared.agent_run import AGENT_TURN_LIMIT  # noqa: E402
from shared.enums import CaseTrigger, ReaderModelChoice, RequestState  # noqa: E402
from shared.reader_agent import BEDROCK_MODEL_IDS  # noqa: E402
from tests.helpers.scripted_model import ScriptedModel, tool_call_step, verdict_step  # noqa: E402
from tests.test_verdict import aws  # noqa: E402,F401

import eval_agents  # noqa: E402
from eval_cases import (  # noqa: E402
    ALL_EVAL_CASES, CALIBRATION_ANSWERS, CHAT_QUESTIONS, CONTACT_NAME, EVAL_CASES_BY_ID, ChatQuestionKind, FounderLabel,
    JudgeRule, JudgeVerdict,
)

CASE_AGENT_MODEL = ReaderModelChoice.OPUS_4_5  # infra/cdk.json caseAgentModel
PHONE_NUMBER_REPLY = "Jimin said that email was not from him. You can call him at 1-800-555-0199."
EARLIER_CITING_NOTE = "Jimin said this email was not from him, so Pidge kept it out. A similar email was held for you before."
LINK_NOTE = "Your card company can help. Start at bank-help.example/refund and follow the steps in order."
FROZEN_TEST_CREDENTIALS = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}


def judge_fields(broken_rule: JudgeRule | None) -> dict:
    return {str(rule): rule is broken_rule for rule in JudgeRule}


def calibration_judge_scripts(mislabelled_calibration_id: str | None = None) -> dict:
    """The judge agrees with every founder label, except on the one id given, where it flips pass and fail."""
    scripts = {}
    for calibration in CALIBRATION_ANSWERS:
        judge_fails = (calibration.founder_label is FounderLabel.FAIL) != (calibration.calibration_id == mislabelled_calibration_id)
        flagged_rule = calibration.broken_rule if calibration.broken_rule is not None else JudgeRule.INVENTS_A_FACT
        scripts[calibration.answer_text] = [verdict_step(judge_fields(flagged_rule if judge_fails else None))]
    return scripts


class ScriptedAgents:
    """Wraps the production builders and swaps each agent's model for a ScriptedModel, as test_agent_run does."""

    def __init__(self, case_scripts: dict, chat_scripts: dict, judge_scripts: dict) -> None:
        self.case_model = ScriptedModel(case_agent.CaseNote, case_scripts)
        self.chat_model = ScriptedModel(chat_agent.ChatReply, chat_scripts)
        self.judge_model = ScriptedModel(JudgeVerdict, judge_scripts)
        self.built_agent_count = 0

    def build_case_agent(self, scope: case_agent.CaseScope):
        agent = case_agent.build_case_agent(CASE_AGENT_MODEL, scope)
        agent.model = self.case_model
        self.built_agent_count += 1
        return agent

    def build_chat_agent(self, user_id: str):
        agent = chat_agent.build_chat_agent(CASE_AGENT_MODEL, user_id)
        agent.model = self.chat_model
        self.built_agent_count += 1
        return agent

    def build_judge(self):
        judge = eval_agents.build_judge_agent()
        judge.model = self.judge_model
        return judge

    def factories(self) -> eval_agents.EvalAgentFactories:
        return eval_agents.EvalAgentFactories(self.build_case_agent, self.build_chat_agent, self.build_judge)

    def raised_errors(self) -> list[str]:
        return self.case_model.raised_errors + self.chat_model.raised_errors + self.judge_model.raised_errors


def run_scripted_eval(case_ids: list[str], agents: ScriptedAgents, report_directory: Path) -> eval_agents.EvalExitCode:
    eval_cases = [EVAL_CASES_BY_ID[case_id] for case_id in case_ids]
    return eval_agents.run_eval(eval_cases, list(CALIBRATION_ANSWERS), agents.factories(), CASE_AGENT_MODEL, report_directory)


def read_latest_report(report_directory: Path) -> dict:
    return json.loads((report_directory / "latest.json").read_text())


def holds_answer_inside_boundary_tags(prompt_text: str, answer_text: str) -> bool:
    return re.search(rf"<(answer-[0-9a-f]{{16}})>\n{re.escape(answer_text)}\n</\1>", prompt_text) is not None


def test_mock_config_passes_only_bedrock_runtime_through(monkeypatch):
    expected_config = {"core": {"mock_credentials": False, "passthrough": {"services": ["bedrockruntime"]}}}
    assert eval_agents.MOCK_AWS_CONFIG == expected_config
    assert "bedrockruntime" in dict(moto.backend_index.backend_url_patterns)  # the passthrough key is one moto knows
    recorded_configs = []
    real_mock_aws = eval_agents.mock_aws
    monkeypatch.setattr(eval_agents, "mock_aws", lambda config: recorded_configs.append(config) or real_mock_aws(config=config))
    monkeypatch.setenv("AWS_PROFILE", "a-profile-that-would-resolve-inside-the-mock")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "a-stale-token")
    with eval_agents.open_offline_sandbox(FROZEN_TEST_CREDENTIALS):
        assert os.environ["TABLE_NAME"] == eval_agents.EVAL_TABLE_NAME != eval_agents.LIVE_TABLE_NAME
        assert "AWS_PROFILE" not in os.environ and "AWS_SESSION_TOKEN" not in os.environ
        assert all(os.environ[name] == value for name, value in FROZEN_TEST_CREDENTIALS.items())
        assert graph.get_table().table_name == eval_agents.EVAL_TABLE_NAME
        assert graph.get_table().item_count == 0  # answered by moto: the table exists only inside the mock
    assert recorded_configs == [expected_config]
    assert os.environ["AWS_PROFILE"] == "a-profile-that-would-resolve-inside-the-mock"  # restored on exit


def test_production_factories_use_the_cdk_model_and_the_judge_has_no_tools():
    cdk_model = eval_agents.case_agent_model_from_cdk_context()
    factories = eval_agents.production_agent_factories(cdk_model)
    assert (factories.build_case_agent.func, factories.build_case_agent.args) == (case_agent.build_case_agent, (cdk_model,))
    assert (factories.build_chat_agent.func, factories.build_chat_agent.args) == (chat_agent.build_chat_agent, (cdk_model,))
    judge = eval_agents.build_judge_agent()
    assert judge.tool_names == []
    assert eval_agents.JUDGE_MODEL_CHOICE is ReaderModelChoice.OPUS_4_5
    assert judge.model.get_config()["model_id"] == BEDROCK_MODEL_IDS[ReaderModelChoice.OPUS_4_5]


def test_a_calibration_disagreement_stops_before_any_agent_run(aws, tmp_path):  # noqa: F811
    agents = ScriptedAgents({}, {}, calibration_judge_scripts(mislabelled_calibration_id="calibration-1"))
    exit_code = run_scripted_eval([eval_case.case_id for eval_case in ALL_EVAL_CASES], agents, tmp_path)
    assert exit_code is eval_agents.EvalExitCode.CALIBRATION_DISAGREED and int(exit_code) == 2
    assert agents.built_agent_count == 0
    assert agents.case_model.recorded_calls == [] and agents.chat_model.recorded_calls == []
    assert len(agents.judge_model.recorded_calls) == len(CALIBRATION_ANSWERS) == 8
    assert agents.raised_errors() == []
    assert list(tmp_path.iterdir()) == []


def test_a_fail_flagged_for_the_wrong_rule_stops_the_run(aws, tmp_path):  # noqa: F811
    [contradicting_answer] = [calibration for calibration in CALIBRATION_ANSWERS if calibration.calibration_id == "calibration-2"]
    assert contradicting_answer.broken_rule is JudgeRule.CONTRADICTS_WHAT_HAPPENED
    judge_scripts = {**calibration_judge_scripts(),
                     contradicting_answer.answer_text: [verdict_step(judge_fields(JudgeRule.INVENTS_A_FACT))]}
    agents = ScriptedAgents({}, {}, judge_scripts)
    exit_code = run_scripted_eval([eval_case.case_id for eval_case in ALL_EVAL_CASES], agents, tmp_path)
    assert exit_code is eval_agents.EvalExitCode.CALIBRATION_DISAGREED
    assert agents.built_agent_count == 0
    assert agents.case_model.recorded_calls == [] and agents.chat_model.recorded_calls == []
    assert agents.raised_errors() == []


def test_an_unsafe_answer_fails_whatever_the_judge_says(aws, tmp_path):  # noqa: F811
    judge_scripts = {**calibration_judge_scripts(), PHONE_NUMBER_REPLY: [verdict_step(judge_fields(None))]}
    agents = ScriptedAgents({}, {CHAT_QUESTIONS[ChatQuestionKind.ANSWERABLE]: [
        tool_call_step("list_my_held_emails", {}), verdict_step({"reply_text": PHONE_NUMBER_REPLY})]}, judge_scripts)
    exit_code = run_scripted_eval(["chat-answerable"], agents, tmp_path)
    assert exit_code is eval_agents.EvalExitCode.A_CASE_FAILED
    [case_row] = read_latest_report(tmp_path)["cases"]
    assert (case_row["case_id"], case_row["answer_text"]) == ("chat-answerable", PHONE_NUMBER_REPLY)
    assert case_row["judge_verdict"] == judge_fields(None)  # the judge passed it
    assert (case_row["unsafe_kinds"], case_row["failures"], case_row["passed"]) == (["long_number"], ["unsafe_span"], False)
    assert len(agents.judge_model.recorded_calls) == len(CALIBRATION_ANSWERS) + 1
    assert agents.raised_errors() == []


def test_report_counts_match_a_fixed_three_case_fixture(aws, tmp_path):  # noqa: F811
    earlier_scope_prompt = case_agent.run_prompt(case_agent.CaseScope("any", "any", CaseTrigger.VERDICT))
    payment_scope_prompt = case_agent.run_prompt(case_agent.CaseScope("any", "any", CaseTrigger.CASE, "any"))
    case_scripts = {
        earlier_scope_prompt: [tool_call_step("get_case", {}), tool_call_step("get_family_history", {}),
                               verdict_step({"note_text": EARLIER_CITING_NOTE, "cites_previous_case": True})],
        payment_scope_prompt: [verdict_step({"note_text": LINK_NOTE, "cites_previous_case": True})],
    }
    # The chat agent never answers: one tool call per turn until the cap, so it is not counted as answered.
    chat_scripts = {CHAT_QUESTIONS[ChatQuestionKind.ABOUT_A_CASE]: [tool_call_step("list_my_cases", {})] * (AGENT_TURN_LIMIT + 1)}
    judge_scripts = {**calibration_judge_scripts(), EARLIER_CITING_NOTE: [verdict_step(judge_fields(None))],
                     LINK_NOTE: [verdict_step(judge_fields(None))]}
    agents = ScriptedAgents(case_scripts, chat_scripts, judge_scripts)
    exit_code = run_scripted_eval(["note-denied-after-earlier", "note-reported-payment", "chat-about-a-case"], agents, tmp_path)
    assert exit_code is eval_agents.EvalExitCode.A_CASE_FAILED
    report = read_latest_report(tmp_path)
    assert report["counts"] == {"cases": 3, "passed": 1, "failed": 2, "answered": 2, "refused": 1, "refusal_rate": 1 / 2}
    assert [(row["case_id"], row["failures"], row["agent_run_failure"]) for row in report["cases"]] == [
        ("note-denied-after-earlier", [], None),
        ("note-reported-payment", ["unsafe_span", "cites_previous_case_mismatch"], None),
        ("chat-about-a-case", ["agent_run_failed"], "turn_limit"),
    ]
    assert [row["agrees"] for row in report["calibration"]] == [True] * 8
    judge_calls = agents.judge_model.recorded_calls
    graded_answers = [calibration.answer_text for calibration in CALIBRATION_ANSWERS] + [EARLIER_CITING_NOTE, LINK_NOTE]
    assert len(judge_calls) == len(graded_answers) == 10
    assert all(call.user_text_count == 1 for call in judge_calls)  # the history is cleared before every grade
    assert all(holds_answer_inside_boundary_tags(call.prompt_text, answer_text)
               for call, answer_text in zip(judge_calls, graded_answers))
    denied_sentence = case_agent.OUTCOME_SENTENCES[RequestState.DENIED].format(contact=CONTACT_NAME)
    assert all(denied_sentence in call.prompt_text for call in judge_calls[-2:])  # both graded notes are DENIED cases
    [saved_report_path] = [path for path in tmp_path.iterdir() if path.name.startswith("REPORT-")]
    assert saved_report_path.name == f"REPORT-{report['run_id']}.json" and json.loads(saved_report_path.read_text()) == report
    assert agents.built_agent_count == 3 and agents.raised_errors() == []


def test_a_crash_exits_with_run_crashed_by_type_only_and_leaves_no_latest_report(monkeypatch, tmp_path, capsys):
    (tmp_path / "latest.json").write_text("{}")
    monkeypatch.setattr(eval_agents, "REPORT_DIRECTORY", tmp_path)

    def raise_with_model_text() -> ReaderModelChoice:
        raise RuntimeError("model said https://evil.example")

    monkeypatch.setattr(eval_agents, "case_agent_model_from_cdk_context", raise_with_model_text)  # the first guard main runs
    exit_code = eval_agents.main()
    assert exit_code == eval_agents.EvalExitCode.RUN_CRASHED
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err  # main prints the crash to stderr
    assert "evil.example" not in captured.out + captured.err
    assert not (tmp_path / "latest.json").exists()


def test_a_short_calibration_list_raises_before_any_model_call(monkeypatch, tmp_path):
    monkeypatch.setenv("TABLE_NAME", eval_agents.EVAL_TABLE_NAME)
    agents = ScriptedAgents({}, {}, calibration_judge_scripts())
    seven_answers = list(CALIBRATION_ANSWERS)[:7]
    with pytest.raises(ValueError):
        eval_agents.run_eval(list(ALL_EVAL_CASES), seven_answers, agents.factories(), CASE_AGENT_MODEL, tmp_path)
    assert agents.built_agent_count == 0
    assert agents.case_model.recorded_calls == agents.chat_model.recorded_calls == agents.judge_model.recorded_calls == []
