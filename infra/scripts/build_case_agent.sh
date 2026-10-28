#!/usr/bin/env bash
# Builds the case agent's direct-code deployment directory for AgentCore Runtime (Linux arm64, Python 3.12), no Docker.
# Layout: dependencies at the root, main.py at the root, lambda/shared copied in as shared/.
set -euo pipefail
repository_root="$(cd "$(dirname "$0")/../.." && pwd)"
build_directory="$repository_root/infra/build/case_agent"
rm -rf "$build_directory"
mkdir -p "$build_directory"
uv pip install \
  --requirement "$repository_root/agent/pidge_case/requirements.txt" \
  --target "$build_directory" \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.12 \
  --only-binary :all: \
  --quiet
# uv writes the local venv's absolute python path into each console script; the Runtime only has python3 on PATH.
sed -i '' '1s|^#!.*python.*$|#!/usr/bin/env python3|' "$build_directory"/bin/*
# A long build path makes uv write a /bin/sh trampoline instead, which the sed above cannot fix: stop the build.
if grep -l -E "(/Users/|/private/|/tmp/)" "$build_directory"/bin/*; then
  echo "build_case_agent.sh: a console script above still points at a local python; build from a shorter path" >&2
  exit 1
fi
cp "$repository_root/agent/pidge_case/main.py" "$build_directory/main.py"
cp -R "$repository_root/lambda/shared" "$build_directory/shared"
find "$build_directory" -type d \( -name __pycache__ -o -name tests \) -prune -exec rm -rf {} +
find "$build_directory" -type d -exec chmod 755 {} +
find "$build_directory" -type f -exec chmod 644 {} +
find "$build_directory/bin" -type f -exec chmod 755 {} + 2>/dev/null || true
du -sh "$build_directory"
