"""The one runner caps every tool-holding agent run: the real case agent and Strands loop on the moto table, with a
scripted model in place of Bedrock."""

import traceback
from types import SimpleNamespace

import pytest
from strands.types.exceptions import StructuredOutputException

from shared import case_agent
from shared.agent_run import AGENT_TURN_LIMIT, AgentRunError, run_agent_once
from shared.agent_enums import AgentRunFailure
from shared.enums import CaseTrigger, ReaderModelChoice
from tests.helpers.scripted_model import ScriptedModel, tool_call_step, verdict_step
from tests.test_case_agent import denied_request_with_body, report
from tests.test_refund_rules import seed_rules
from tests.test_verdict import RECEIVER_USER_ID, aws  # noqa: F401

PACK_NOTE_TEXT = "Jimin said the email was not from him. Call your card company first, then follow the other steps in order."


def case_agent_with_scripted_model(scope: case_agent.CaseScope, steps: list) -> tuple:
    agent = case_agent.build_case_agent(ReaderModelChoice.HAIKU_4_5, scope)
    scripted_model = ScriptedModel(case_agent.CaseNote, {case_agent.run_prompt(scope): steps})
    agent.model = scripted_model
    return agent, scripted_model


def tool_result_statuses(agent) -> list[str]:
    return [content_block["toolResult"]["status"] for message in agent.messages if message["role"] == "user"
            for content_block in message["content"] if "toolResult" in content_block]


def test_a_tool_loop_stops_at_the_turn_limit(aws):  # noqa: F811
    scope = case_agent.CaseScope(RECEIVER_USER_ID, denied_request_with_body(), CaseTrigger.VERDICT)
    spare_step_count = 2
    agent, scripted_model = case_agent_with_scripted_model(scope, [tool_call_step("get_case", {})] * (AGENT_TURN_LIMIT + spare_step_count))
    with pytest.raises(AgentRunError) as raised:
        case_agent.run_case_agent(agent, scope)
    assert raised.value.failure is AgentRunFailure.TURN_LIMIT
    assert AGENT_TURN_LIMIT == 8 and len(scripted_model.recorded_calls) == 8
    assert scripted_model.remaining_steps_by_fragment[case_agent.run_prompt(scope)] == [tool_call_step("get_case", {})] * spare_step_count
    assert scripted_model.raised_errors == []
    assert tool_result_statuses(agent) == ["success"] * 8  # the real get_case ran on every turn


def test_a_five_turn_case_run_returns_its_note_under_the_limit(aws):  # noqa: F811
    seed_rules()
    request_id = denied_request_with_body()
    case_row = report(request_id)
    scope = case_agent.CaseScope(RECEIVER_USER_ID, request_id, CaseTrigger.CASE, case_row["PK"].removeprefix("CASE#"))
    steps = [tool_call_step("get_case", {}), tool_call_step("get_family_history", {}),
             tool_call_step("read_refund_rule", {"jurisdiction": "US", "payment_rail": "card"}),
             tool_call_step("draft_recovery_pack", {}),
             verdict_step({"note_text": PACK_NOTE_TEXT, "cites_previous_case": False})]
    agent, scripted_model = case_agent_with_scripted_model(scope, steps)
    note = case_agent.run_case_agent(agent, scope)
    assert note == case_agent.CaseNote(note_text=PACK_NOTE_TEXT, cites_previous_case=False)
    assert len(scripted_model.recorded_calls) == 5 < AGENT_TURN_LIMIT
    assert [call.is_forced_retry for call in scripted_model.recorded_calls] == [False] * 5
    assert scripted_model.raised_errors == []
    assert tool_result_statuses(agent)[:4] == ["success"] * 4


def test_a_run_that_ends_without_its_output_raises_no_output():
    """Strands' own end-of-turn retry is bypassed here: an agent whose result carries no output must never yield None."""
    def agent_without_output(prompt, structured_output_model, limits):
        return SimpleNamespace(stop_reason="end_turn", structured_output=None)

    with pytest.raises(AgentRunError) as raised:
        run_agent_once(agent_without_output, "any prompt", case_agent.CaseNote)
    assert raised.value.failure is AgentRunFailure.NO_OUTPUT


def test_a_structured_output_failure_raises_no_output_without_the_text():
    """Strands quotes the invalid model output in the exception; the AgentRunError and its printed chain carry none of it."""
    def agent_with_invalid_output(prompt, structured_output_model, limits):
        raise StructuredOutputException("validation failed: input_value='Call 010-1234-5678 now'")

    with pytest.raises(AgentRunError) as raised:
        run_agent_once(agent_with_invalid_output, "any prompt", case_agent.CaseNote)
    assert raised.value.failure is AgentRunFailure.NO_OUTPUT
    printed_chain = "".join(traceback.format_exception(raised.value))
    assert "010-1234-5678" not in str(raised.value) and "010-1234-5678" not in printed_chain
