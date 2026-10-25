"""Grep gate: file size, empty except/catch and `?? <default>` fallbacks across the source tree.

Known hits live in baseline.json (keyed by path, rule and the matched text, not the line number, so a refactor
does not churn it). A hit outside the baseline fails the gate; a baseline entry that no longer matches is reported.
Usage: python scripts/checks/grep_gate.py [--write-baseline]   (run from anywhere; lambda/tests/test_grep_gate.py runs it)
"""

import json
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_FILE = Path(__file__).resolve().parent / "baseline.json"
SOURCE_LINE_LIMIT = 500
TEST_LINE_LIMIT = 600
SOURCE_GLOBS = ("lambda/**/*.py", "agent/**/*.py", "scripts/**/*.py", "pwa/js/*.js", "pwa/sw.js", "infra/lib/*.ts", "infra/bin/*.ts")
SKIPPED_PATH_PARTS = {".venv", "node_modules", "build", "cdk.out", "__pycache__"}
NULLISH_WITH_DEFAULT = re.compile(r"\?\?\s*(?!null\b|undefined\b)\S")
EMPTY_CATCH = re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}")
PYTHON_EXCEPT = re.compile(r"^\s*except\b.*:\s*(pass\s*)?$")


class GateRule(StrEnum):
    FILE_TOO_LONG = "file_too_long"
    EMPTY_EXCEPT = "empty_except"
    EMPTY_CATCH = "empty_catch"
    NULLISH_DEFAULT = "nullish_default"


@dataclass(frozen=True)
class GateHit:
    path: str
    rule: GateRule
    text: str
    line: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.rule}::{self.text}"


def list_source_files() -> list[Path]:
    files: list[Path] = []
    for glob in SOURCE_GLOBS:
        files.extend(path for path in REPOSITORY_ROOT.glob(glob) if not SKIPPED_PATH_PARTS.intersection(path.parts))
    return sorted(set(files))


def is_test_file(relative_path: str) -> bool:
    return "/tests/" in relative_path or relative_path.endswith("conftest.py")


def find_empty_python_excepts(lines: list[str]) -> list[tuple[int, str]]:
    hits = []
    for index, line in enumerate(lines):
        if not PYTHON_EXCEPT.match(line):
            continue
        if line.rstrip().endswith("pass"):
            hits.append((index + 1, line.strip()))
            continue
        following = next((candidate for candidate in lines[index + 1:] if candidate.strip()), "")
        if following.strip() == "pass":
            hits.append((index + 1, line.strip()))
    return hits


def find_hits_in_file(path: Path) -> list[GateHit]:
    relative_path = path.relative_to(REPOSITORY_ROOT).as_posix()
    lines = path.read_text().splitlines()
    hits: list[GateHit] = []
    limit = TEST_LINE_LIMIT if is_test_file(relative_path) else SOURCE_LINE_LIMIT
    if len(lines) > limit:
        hits.append(GateHit(relative_path, GateRule.FILE_TOO_LONG, f"over {limit} lines", len(lines)))
    if path.suffix == ".py":
        hits.extend(GateHit(relative_path, GateRule.EMPTY_EXCEPT, text, line) for line, text in find_empty_python_excepts(lines))
        return hits
    for index, line in enumerate(lines):
        if EMPTY_CATCH.search(line):
            hits.append(GateHit(relative_path, GateRule.EMPTY_CATCH, line.strip(), index + 1))
        if NULLISH_WITH_DEFAULT.search(line) and not line.lstrip().startswith("//"):
            hits.append(GateHit(relative_path, GateRule.NULLISH_DEFAULT, line.strip(), index + 1))
    return hits


def main(arguments: list[str]) -> int:
    hits = [hit for path in list_source_files() for hit in find_hits_in_file(path)]
    if "--write-baseline" in arguments:
        BASELINE_FILE.write_text(json.dumps(sorted(hit.key for hit in hits), indent=2) + "\n")
        print(f"baseline written: {len(hits)} known hit(s)")
        return 0
    baseline = set(json.loads(BASELINE_FILE.read_text())) if BASELINE_FILE.exists() else set()
    new_hits = [hit for hit in hits if hit.key not in baseline]
    stale_keys = baseline - {hit.key for hit in hits}
    for key in sorted(stale_keys):
        print(f"STALE baseline entry no longer matches: {key}")
    for hit in new_hits:
        print(f"FAIL {hit.path}:{hit.line} {hit.rule}: {hit.text}")
    print(f"grep gate: {len(hits)} hit(s), {len(hits) - len(new_hits)} in baseline, {len(new_hits)} new, {len(stale_keys)} stale")
    return 1 if new_hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
