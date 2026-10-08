"""Migration scripts render offline, without a database."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]


def _config(buffer: StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_history_is_a_single_linear_chain_from_the_baseline() -> None:
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == script.get_heads()[:1]
    assert [rev.revision for rev in script.walk_revisions()][-1] == "0001"


def test_baseline_sql_is_idempotent_for_the_sentinel_row() -> None:
    buffer = StringIO()
    command.upgrade(_config(buffer), "head", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE app_meta" in sql
    assert "ON CONFLICT (key) DO NOTHING" in sql


def test_operations_schema_enforces_single_active_run_and_append_only_history() -> None:
    buffer = StringIO()
    command.upgrade(_config(buffer), "0001:0002", sql=True)
    sql = buffer.getvalue()
    assert "CREATE UNIQUE INDEX ux_runs_one_active_per_session" in sql
    assert "WHERE status IN ('running', 'waiting_for_input', 'cancelling')" in sql
    for table in ("execution_events", "run_events"):
        assert f"CREATE TRIGGER {table}_append_only BEFORE UPDATE" in sql


def test_operations_schema_downgrades_offline() -> None:
    buffer = StringIO()
    command.downgrade(_config(buffer), "0002:0001", sql=True)
    sql = buffer.getvalue()
    assert "DROP TABLE run_events" in sql
    assert "DROP TABLE sessions" in sql


def test_access_schema_constrains_roles_and_product_ids() -> None:
    buffer = StringIO()
    command.upgrade(_config(buffer), "0002:0003", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE executives" in sql
    assert "CONSTRAINT uq_executives_identity UNIQUE (issuer, subject)" in sql
    assert "roles <@ ARRAY['executive', 'editor', 'reviewer', 'admin']" in sql
    assert "CREATE TABLE product_entitlements" in sql
    assert "PRIMARY KEY (executive_id, product_id)" in sql

    buffer = StringIO()
    command.downgrade(_config(buffer), "0003:0002", sql=True)
    assert "DROP TABLE product_entitlements" in buffer.getvalue()


def test_query_job_submissions_schema_upgrades_and_downgrades_offline() -> None:
    buffer = StringIO()
    command.upgrade(_config(buffer), "0007:0008", sql=True)
    sql = buffer.getvalue()
    assert "ADD COLUMN submission INTEGER DEFAULT '1' NOT NULL" in sql
    assert "PRIMARY KEY (operation_id, submission)" in sql
    assert "CHECK (submission >= 1)" in sql

    buffer = StringIO()
    command.downgrade(_config(buffer), "0008:0007", sql=True)
    sql = buffer.getvalue()
    assert "DELETE FROM query_executions WHERE submission > 1" in sql
    assert "DROP COLUMN submission" in sql


def test_topic_resets_schema_upgrades_and_downgrades_offline() -> None:
    buffer = StringIO()
    command.upgrade(_config(buffer), "0009:0010", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE topic_resets" in sql
    assert "REFERENCES sessions (session_id) ON DELETE CASCADE" in sql

    buffer = StringIO()
    command.downgrade(_config(buffer), "0010:0009", sql=True)
    assert "DROP TABLE topic_resets" in buffer.getvalue()


def test_run_budget_schema_upgrades_and_downgrades_offline() -> None:
    buffer = StringIO()
    command.upgrade(_config(buffer), "0010:0011", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE run_budgets" in sql
    assert "CREATE TABLE budget_charges" in sql
    assert "PRIMARY KEY (run_id, kind, charge_key)" in sql
    assert "(kind = 'correction') = (group_key IS NOT NULL)" in sql

    buffer = StringIO()
    command.downgrade(_config(buffer), "0011:0010", sql=True)
    sql = buffer.getvalue()
    assert "DROP TABLE budget_charges" in sql
    assert "DROP TABLE run_budgets" in sql


def test_investigation_input_schema_upgrades_and_downgrades_offline() -> None:
    buffer = StringIO()
    command.upgrade(_config(buffer), "0011:0012", sql=True)
    sql = buffer.getvalue()
    for table in ("run_principals", "run_inputs", "run_questions"):
        assert f"CREATE TABLE {table}" in sql
    assert "(kind = 'queued') = (run_id IS NULL)" in sql
    assert "CREATE UNIQUE INDEX ux_run_questions_one_open" in sql

    buffer = StringIO()
    command.downgrade(_config(buffer), "0012:0011", sql=True)
    sql = buffer.getvalue()
    for table in ("run_principals", "run_inputs", "run_questions"):
        assert f"DROP TABLE {table}" in sql
