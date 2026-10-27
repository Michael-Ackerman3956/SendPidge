"""The grep gate (file size, empty except/catch, `?? default`, loaded-row defaults, agent calls outside the runner) runs with
the tests; a hit outside scripts/checks/baseline.json fails."""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
GREP_GATE_SCRIPT = REPOSITORY_ROOT / "scripts" / "checks" / "grep_gate.py"


def test_grep_gate_has_no_hits_outside_the_baseline():
    completed = subprocess.run([sys.executable, str(GREP_GATE_SCRIPT)], capture_output=True, text=True, cwd=REPOSITORY_ROOT, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def load_grep_gate():
    gate_spec = importlib.util.spec_from_file_location("grep_gate", GREP_GATE_SCRIPT)
    grep_gate = importlib.util.module_from_spec(gate_spec)
    gate_spec.loader.exec_module(grep_gate)
    return grep_gate


def test_loaded_row_default_rule_finds_two_hits_in_a_three_line_snippet():
    grep_gate = load_grep_gate()
    snippet = ['visibility = row.get("visibility", "alerts_only")', 'members = response.get("Items") or []',
               'coverage = row.get("coverage", None)']
    assert [line for line, _text in grep_gate.find_loaded_row_defaults(snippet)] == [1, 2]


def test_agent_call_outside_runner_rule_flags_a_tool_module_call():
    grep_gate = load_grep_gate()
    agent_call = "    result = agent(question, structured_output_model=ChatReply)"
    tool_module = ["from strands import tool", "", "    @tool", "    def list_my_held_emails() -> list[dict]:", agent_call]
    assert grep_gate.find_agent_calls_outside_runner("lambda/shared/chat_agent.py", tool_module) == [(5, agent_call.strip())]
    tool_less_caller = ["def answer(agent, question):", agent_call]  # a handler with no @tool still goes through the runner
    assert grep_gate.find_agent_calls_outside_runner("agent/pidge_case/main.py", tool_less_caller) == [(2, agent_call.strip())]
    assert grep_gate.find_agent_calls_outside_runner("lambda/shared/agent_run.py", tool_less_caller) == []
    assert grep_gate.GateRule.AGENT_CALL_OUTSIDE_RUNNER == "agent_call_outside_runner"
