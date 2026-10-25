"""The grep gate (file size, empty except/catch, `?? default`) runs with the tests; a hit outside scripts/checks/baseline.json fails."""

import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
GREP_GATE_SCRIPT = REPOSITORY_ROOT / "scripts" / "checks" / "grep_gate.py"


def test_grep_gate_has_no_hits_outside_the_baseline():
    completed = subprocess.run([sys.executable, str(GREP_GATE_SCRIPT)], capture_output=True, text=True, cwd=REPOSITORY_ROOT, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
