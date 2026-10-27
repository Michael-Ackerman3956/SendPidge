"""The grep gate (file size, empty except/catch, `?? default`, loaded-row defaults) runs with the tests; a hit outside scripts/checks/baseline.json fails."""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
GREP_GATE_SCRIPT = REPOSITORY_ROOT / "scripts" / "checks" / "grep_gate.py"


def test_grep_gate_has_no_hits_outside_the_baseline():
    completed = subprocess.run([sys.executable, str(GREP_GATE_SCRIPT)], capture_output=True, text=True, cwd=REPOSITORY_ROOT, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_loaded_row_default_rule_finds_two_hits_in_a_three_line_snippet():
    gate_spec = importlib.util.spec_from_file_location("grep_gate", GREP_GATE_SCRIPT)
    grep_gate = importlib.util.module_from_spec(gate_spec)
    gate_spec.loader.exec_module(grep_gate)
    snippet = ['visibility = row.get("visibility", "alerts_only")', 'members = response.get("Items") or []',
               'coverage = row.get("coverage", None)']
    assert [line for line, _text in grep_gate.find_loaded_row_defaults(snippet)] == [1, 2]
