#!/usr/bin/env sh
# Run the worker and the API together for local development (not production):
#   ./scripts/dev.sh [--env-file FILE] [--project NAME] [--no-services] [--no-telemetry]
# Needs the environment created by ./scripts/bootstrap.sh. Ctrl-C stops both.
set -eu
cd "$(dirname "$0")/.."

if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
  PY="$VIRTUAL_ENV/bin/python"
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  echo "No virtualenv found. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi

exec "$PY" -m retail_analytics.bootstrap.dev_up "$@"
