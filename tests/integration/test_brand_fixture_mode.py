"""Fixture-mode brand provisioning end to end on PostgreSQL.

``provision`` then ``sync-brands`` with ``APP_MODE=fixture`` gives the demo
managers disjoint products from the synthetic held-out catalog. Needs Docker
(``pytest -m docker``); throwaway Compose project.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from retail_analytics.bootstrap import dev_access
from retail_analytics.bootstrap.persistence import build_persistence
from tests.integration.compose_stack import Stack, running_stack

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


async def test_fixture_mode_resolves_synthetic_brands_per_manager(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(ROOT)
    env = {
        "APP_MODE": "fixture",
        "APP_DATABASE_URL": stack.app_url,
        "AUTH_SIGNING_KEY": "integration-test-signing-key-" + "z" * 40,
    }
    runner = CliRunner()
    for command in (["provision"], ["sync-brands"]):
        done = await asyncio.to_thread(runner.invoke, dev_access.main, command, env=env)
        assert done.exit_code == 0, done.output
    persistence = build_persistence(stack.app_url)
    try:
        a = await persistence.executives.get("exec-demo-a")
        b = await persistence.executives.get("exec-demo-b")
    finally:
        persistence.close()
    assert a is not None and b is not None
    assert a.product_ids == {"201", "202", "203", "204"}  # Aster, Birch
    assert b.product_ids == {"205", "206", "207"}  # Cedar, Dune
    assert a.product_ids.isdisjoint(b.product_ids)
