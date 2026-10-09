#!/usr/bin/env sh
# Start the local backend for development (not production): the API, which
# runs the investigations itself with local execution (the default), plus the
# Temporal worker when EXECUTION_BACKEND=temporal.
#   ./scripts/dev.sh [--env-file FILE] [--project NAME] [--execution-backend local|temporal]
#                    [--no-services] [--no-telemetry] [--ready-timeout SECONDS]
# Needs the environment created by ./scripts/bootstrap.sh. Ctrl-C stops what it started.
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
