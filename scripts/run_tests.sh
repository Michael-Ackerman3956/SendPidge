#!/usr/bin/env bash
# Runs the whole local suite: pytest (with the grep gate and the CDK synth assertions), then the infra type check.
set -euo pipefail
repository_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repository_root/lambda"
../.venv/bin/python -m pytest -q
cd "$repository_root/infra"
./node_modules/.bin/tsc --noEmit  # the local compiler; npx would fetch one from the registry if it were missing
echo "run_tests.sh: all checks passed"
