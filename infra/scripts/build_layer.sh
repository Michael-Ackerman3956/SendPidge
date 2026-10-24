#!/usr/bin/env bash
# Builds the Python dependency layer for Lambda (x86_64, Python 3.12) without Docker.
# Linux wheels only, except http-ece (pywebpush's pure-Python dependency publishes no wheel, so it builds from source).
set -euo pipefail
repository_root="$(cd "$(dirname "$0")/../.." && pwd)"
layer_directory="$repository_root/infra/build/layer"
rm -rf "$layer_directory"
mkdir -p "$layer_directory/python"
uv pip install \
  --requirement "$repository_root/lambda/requirements.txt" \
  --target "$layer_directory/python" \
  --python-platform x86_64-manylinux2014 \
  --python-version 3.12 \
  --only-binary :all: \
  --no-binary http-ece \
  --quiet
find "$layer_directory" -type d \( -name __pycache__ -o -name tests \) -prune -exec rm -rf {} +
du -sh "$layer_directory"
