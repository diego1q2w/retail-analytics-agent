#!/bin/sh
# Runs once, on first initialisation of an empty data volume (official postgres
# image convention), as the db_admin superuser. Creates distinct roles and
# databases so the application cannot reach Temporal's internal tables and
# Temporal cannot reach application data.
set -eu

: "${RETAIL_APP_PASSWORD:?}" "${TEMPORAL_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v app_password="$RETAIL_APP_PASSWORD" \
  -v temporal_password="$TEMPORAL_DB_PASSWORD" <<'SQL'
CREATE ROLE retail_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE temporal LOGIN PASSWORD :'temporal_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

CREATE DATABASE retail_app OWNER retail_app;
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;

-- Databases are connectable by PUBLIC by default; only the owning role (and the
-- superuser) may connect to each.
REVOKE CONNECT ON DATABASE retail_app FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal_visibility FROM PUBLIC;
GRANT CONNECT ON DATABASE retail_app TO retail_app;
GRANT CONNECT ON DATABASE temporal TO temporal;
GRANT CONNECT ON DATABASE temporal_visibility TO temporal;
SQL
