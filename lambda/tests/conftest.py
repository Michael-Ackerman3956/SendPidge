"""Fixtures shared across test modules: the CDK synth runs once per session for test_stack.py and test_stack_org.py."""
import json

import pytest


@pytest.fixture(scope="session")
def synthesized_template(tmp_path_factory) -> dict:
    from tests.test_stack import run_synth_for_tests  # only the stack tests pay for importing the synth helpers
    mirror_root = tmp_path_factory.mktemp("synth_mirror")
    completed = run_synth_for_tests(mirror_root)
    assert completed.returncode == 0, f"synth failed:\n{completed.stderr[-4000:]}"
    return json.loads((mirror_root / "cdk.out" / "SendPidge.template.json").read_text())
