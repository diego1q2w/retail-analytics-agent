#!/bin/sh
# One-shot, idempotent: give MLflow its own role and database on the shared
# PostgreSQL server. Works on an existing data volume (the initdb scripts only
# run on an empty one). MLflow never receives application or Temporal credentials.
set -eu

: "${PGHOST:?}" "${PGPASSWORD:?}" "${MLFLOW_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 --username db_admin --dbname postgres \
  -v mlflow_password="$MLFLOW_DB_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE mlflow LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mlflow')
\gexec
ALTER ROLE mlflow PASSWORD :'mlflow_password';
SELECT 'CREATE DATABASE mlflow OWNER mlflow'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow')
\gexec
REVOKE CONNECT ON DATABASE mlflow FROM PUBLIC;
GRANT CONNECT ON DATABASE mlflow TO mlflow;
SQL
