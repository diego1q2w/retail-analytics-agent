#!/usr/bin/env sh
# Open the local chat as the local administrator (development only):
#   ./scripts/local_cli.sh [analytics chat options, e.g. --resume]
# Start the backend first with ./scripts/dev.sh in another terminal. Each run
# issues a fresh local-admin token into ~/.analytics-token (mode 0600) and then
# runs `analytics chat`. It starts no services and changes no setup.
set -eu
cd "$(dirname "$0")/.."

BIN=.venv/bin
if [ ! -x "$BIN/analytics" ] || [ ! -x "$BIN/retail-analytics-dev-access" ]; then
  echo "No virtualenv found. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi
if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
  echo "HOME is not set to a directory; cannot place the token file." >&2
  exit 1
fi

TOKEN_FILE="$HOME/.analytics-token"
if [ -L "$TOKEN_FILE" ]; then
  echo "$TOKEN_FILE is a symbolic link; refusing to write through it. Remove it and rerun." >&2
  exit 1
fi
if [ -e "$TOKEN_FILE" ] && [ ! -f "$TOKEN_FILE" ]; then
  echo "$TOKEN_FILE exists and is not a regular file; remove it and rerun." >&2
  exit 1
fi

umask 077
TMP=$(mktemp "$HOME/.analytics-token.XXXXXX")
trap 'rm -f "$TMP"' EXIT HUP INT TERM

if ! "$BIN/retail-analytics-dev-access" token local-admin >"$TMP"; then
  echo "Could not issue the local-admin token (see the message above)." >&2
  echo "Run ./scripts/bootstrap.sh to set up the environment and the local admin, then retry." >&2
  exit 1
fi
if [ ! -s "$TMP" ]; then
  echo "The token command returned nothing. Run ./scripts/bootstrap.sh, then retry." >&2
  exit 1
fi
chmod 600 "$TMP"
mv -f "$TMP" "$TOKEN_FILE"
trap - EXIT HUP INT TERM

# The fresh file wins over any CLI_TOKEN inherited from the caller's shell.
unset CLI_TOKEN
CLI_TOKEN_FILE="$TOKEN_FILE"
export CLI_TOKEN_FILE
exec "$BIN/analytics" chat "$@"
