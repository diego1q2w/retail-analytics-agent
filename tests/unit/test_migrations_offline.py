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
