#!/bin/sh
# Apply Temporal's bundled PostgreSQL schemas to the databases created by the
# Postgres init script. Connects as the temporal role, never as the admin.
set -eu
: "${TEMPORAL_DB_HOST:?}" "${TEMPORAL_DB_USER:?}" "${TEMPORAL_DB_PASSWORD:?}"
SCHEMA_DIR=/etc/temporal/schema/postgresql/v12

sql_tool() {
  db="$1"; shift
  temporal-sql-tool --plugin postgres12 --ep "$TEMPORAL_DB_HOST" -p 5432 \
    -u "$TEMPORAL_DB_USER" --pw "$TEMPORAL_DB_PASSWORD" --db "$db" "$@"
}

for pair in "temporal:temporal" "temporal_visibility:visibility"; do
  db="${pair%%:*}"; dir="${pair##*:}"
  sql_tool "$db" setup-schema -v 0.0 >/dev/null 2>&1 || true   # no-op once versioned
  sql_tool "$db" update-schema -d "$SCHEMA_DIR/$dir/versioned" 2>&1 | tail -n 1
done
echo "temporal schemas up to date"
