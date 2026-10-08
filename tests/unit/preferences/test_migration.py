"""Preference memory schema renders offline and constrains what it can hold."""

from __future__ import annotations

from io import StringIO

from alembic import command

from tests.unit.test_migrations_offline import _config


def test_preference_schema_upgrades_and_downgrades_offline() -> None:
    buffer = StringIO()
    command.upgrade(_config(buffer), "0004:0005", sql=True)
    sql = buffer.getvalue()
    for table in ("user_preferences", "preference_proposals", "preference_events"):
        assert f"CREATE TABLE {table}" in sql
    assert "CREATE UNIQUE INDEX uq_pref_default" in sql
    assert "WHERE scope = 'user_default'" in sql
    assert "scope = 'session' OR source <> 'inferred_session'" in sql
    assert "CREATE TRIGGER preference_events_append_only BEFORE UPDATE" in sql
    # The audit trail carries no value column, so forgetting really forgets.
    events = sql.split("CREATE TABLE preference_events")[1].split(");")[0]
    assert "value" not in events

    buffer = StringIO()
    command.downgrade(_config(buffer), "0005:0004", sql=True)
    assert "DROP TABLE user_preferences" in buffer.getvalue()
