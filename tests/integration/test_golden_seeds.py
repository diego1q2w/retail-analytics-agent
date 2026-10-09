"""Seeding the Golden library into real PostgreSQL through the dev command."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from retail_analytics.application.golden_seed_library import seed_library
from retail_analytics.bootstrap.dev_access import provision_demo_executives
from retail_analytics.bootstrap.persistence import build_persistence
from retail_analytics.bootstrap.seed_knowledge import main
from tests.integration.compose_stack import Stack, running_stack

pytestmark = pytest.mark.docker
ISSUER = "retail-analytics-local"


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


def _run(stack: Stack, tmp_path: Path) -> tuple[int, str]:
    env = {
        "APP_DATABASE_URL": stack.app_url,
        "ARTIFACT_DIR": str(tmp_path / "artifacts"),
    }
    result = CliRunner().invoke(main, [], env=env)
    return result.exit_code, result.output


def test_seeding_needs_the_demo_executives_then_is_idempotent(
    stack: Stack, tmp_path: Path
) -> None:
    code, output = _run(stack, tmp_path)
    assert code != 0 and "provision" in output

    persistence = build_persistence(stack.app_url)
    try:
        asyncio.run(
            provision_demo_executives(
                persistence.access_admin,
                ISSUER,
                brands=persistence.brand_access,
            )
        )
    finally:
        persistence.close()

    keys = [e.key for e in seed_library()]
    code, first = _run(stack, tmp_path)
    assert code == 0, first
    assert [line.split(":")[0] for line in first.splitlines()] == keys
    assert all(": published" in line for line in first.splitlines())

    code, second = _run(stack, tmp_path)
    assert code == 0, second
    assert all(": already_published" in line for line in second.splitlines())

    with psycopg.connect(stack.app_dsn) as conn:
        rows = conn.execute(
            "select status, origin, author_id, reviewed_by from golden_versions"
        ).fetchall()
        events = conn.execute(
            "select action, actor_id from golden_review_events order by 1"
        ).fetchall()
    assert len(rows) == len(keys)
    assert set(rows) == {
        ("published", "project_authored", "exec-demo-a", "exec-demo-b")
    }
    assert events.count(("approve", "exec-demo-b")) == len(keys)
    assert events.count(("submit", "exec-demo-a")) == len(keys)
    assert len(events) == 2 * len(keys)
