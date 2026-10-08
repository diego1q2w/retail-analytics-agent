#!/bin/sh
# Render the config template with the environment, then start the stock server.
# The server image has no template renderer for this config shape, and secrets
# must not be baked into the mounted file.
set -eu
: "${TEMPORAL_DB_HOST:?}" "${TEMPORAL_DB_NAME:?}" "${TEMPORAL_VISIBILITY_DB_NAME:?}" \
  "${TEMPORAL_DB_USER:?}" "${TEMPORAL_DB_PASSWORD:?}"

# Compose passes the same BIND_ON_IP logic as the image entrypoint: resolve it here.
: "${BIND_ON_IP:=$(getent hosts "$(hostname)" | awk '{print $1;}')}"

escape() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }

rendered=/tmp/temporal-config.yaml
sed \
  -e "s|\${TEMPORAL_DB_HOST}|$(escape "$TEMPORAL_DB_HOST")|g" \
  -e "s|\${TEMPORAL_DB_NAME}|$(escape "$TEMPORAL_DB_NAME")|g" \
  -e "s|\${TEMPORAL_VISIBILITY_DB_NAME}|$(escape "$TEMPORAL_VISIBILITY_DB_NAME")|g" \
  -e "s|\${TEMPORAL_DB_USER}|$(escape "$TEMPORAL_DB_USER")|g" \
  -e "s|\${TEMPORAL_DB_PASSWORD}|$(escape "$TEMPORAL_DB_PASSWORD")|g" \
  -e "s|\${BIND_ON_IP}|$(escape "$BIND_ON_IP")|g" \
  /etc/temporal/config.template.yaml >"$rendered"

export BIND_ON_IP TEMPORAL_SERVER_CONFIG_FILE_PATH="$rendered"
exec /etc/temporal/entrypoint.sh
