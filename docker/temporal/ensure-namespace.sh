#!/bin/sh
# Wait for the Temporal frontend, then create the namespace if it is missing.
set -eu
: "${TEMPORAL_ADDRESS:?}" "${TEMPORAL_NAMESPACE:?}" "${TEMPORAL_RETENTION:?}"

i=0
until temporal operator cluster health --address "$TEMPORAL_ADDRESS" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -le 60 ] || { echo "temporal frontend not healthy" >&2; exit 1; }
  sleep 1
done

if temporal operator namespace describe --address "$TEMPORAL_ADDRESS" \
    --namespace "$TEMPORAL_NAMESPACE" >/dev/null 2>&1; then
  echo "namespace $TEMPORAL_NAMESPACE exists"
else
  temporal operator namespace create --address "$TEMPORAL_ADDRESS" \
    --namespace "$TEMPORAL_NAMESPACE" --retention "$TEMPORAL_RETENTION"
fi
