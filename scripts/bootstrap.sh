#!/usr/bin/env sh
# One command for a working, seeded local environment on a new machine:
#   ./scripts/bootstrap.sh [--interactive] [--telemetry] [--env-file FILE] ...
# Step 0 (this file): make sure a Python 3.12 virtualenv with the pinned
# dependencies exists. Everything else is the ordered step list in
# src/retail_analytics/bootstrap/local_setup.py. Safe to rerun.
set -eu
cd "$(dirname "$0")/.."

if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
  PY="$VIRTUAL_ENV/bin/python"
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  echo "[0] creating .venv (Python 3.12 required)"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
  elif command -v python3.12 >/dev/null 2>&1; then
    python3.12 -m venv .venv
  else
    echo "Python 3.12 not found. Install it (or uv: https://docs.astral.sh/uv/) and rerun." >&2
    exit 1
  fi
  PY=.venv/bin/python
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  echo "Python 3.12 is required (found: $("$PY" -c 'import sys; print(sys.version.split()[0])'))." >&2
  exit 1
fi

if ! "$PY" -c 'import alembic, click, dotenv, retail_analytics' >/dev/null 2>&1; then
  echo "[0] installing pinned dependencies from requirements.txt"
  if "$PY" -m pip --version >/dev/null 2>&1; then
    "$PY" -m pip install --quiet -r requirements.txt
  elif command -v uv >/dev/null 2>&1; then
    uv pip install --quiet --python "$PY" -r requirements.txt
  else
    echo "Neither pip nor uv is available for $PY." >&2
    exit 1
  fi
fi

exec "$PY" -m retail_analytics.bootstrap.local_setup "$@"
