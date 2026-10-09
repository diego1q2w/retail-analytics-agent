"""Migration 0015 backfill on real PostgreSQL (Docker): required scopes are
recovered only when the exact set is provable from trusted data.

Reports are saved at head, the T18-F1 tables are dropped (downgrade to 0014,
the state before this change) and the upgrade backfills them again.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration.compose_stack import Stack, running_stack
from tests.integration.test_report_scope_coverage import BASE, ScopeEnv

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


@pytest.fixture
def env(stack: Stack, tmp_path: Path) -> Iterator[ScopeEnv]:
    e = ScopeEnv(stack, tmp_path / "artifacts")
    yield e
    e.db.close()


async def test_backfill_recovers_only_provable_scopes(
    env: ScopeEnv, stack: Stack
) -> None:
    alice, alice_session = await env.executive(BASE)
    unchanged, _ = await env.save(alice, alice_session)
    carol_set = BASE | {"6"}
    carol, carol_session = await env.executive(carol_set)
    changed, _ = await env.save(carol, carol_session)
    # Carol's entitlements change before the migration and nobody holds her
    # old set now: it can no longer be proven, so her report stays strict.
    # (A digest held by anyone proves the set, whoever holds it.)
    await env.entitle(carol, carol_set | {"4"})

    stack.alembic("downgrade", "0014")
    assert env.sql("SELECT to_regclass('report_required_scopes')") == [(None,)]
    stack.migrate()

    assert env.required_products(unchanged) == BASE
    assert env.sql(
        "SELECT count(*) FROM report_required_scopes WHERE report_id = %s", changed
    ) == [(0,)]

    await env.entitle(alice, BASE | {"9"})
    assert await env.readable(alice, alice_session, unchanged)
    assert not await env.readable(carol, carol_session, changed)
    await env.entitle(carol, carol_set)
    assert await env.readable(carol, carol_session, changed)
