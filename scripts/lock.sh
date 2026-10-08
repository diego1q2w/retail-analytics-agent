#!/usr/bin/env sh
# Regenerate the pinned requirements.txt from pyproject.toml (via requirements.in).
# Run after changing any dependency pin. On a merge conflict in requirements.txt,
# resolve pyproject.toml first, then rerun this script instead of hand-merging.
set -eu
cd "$(dirname "$0")/.."
uv pip compile requirements.in \
  --universal \
  --python-version 3.12 \
  --no-annotate \
  --custom-compile-command "./scripts/lock.sh" \
  --output-file requirements.txt
