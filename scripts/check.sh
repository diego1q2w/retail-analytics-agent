#!/usr/bin/env sh
# Run every repository check. All must pass before a task commit.
# Uses the active virtualenv (or .venv/bin if present).
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then PATH="$PWD/.venv/bin:$PATH"; fi
ruff check .
ruff format --check .
mypy
python -m pytest
