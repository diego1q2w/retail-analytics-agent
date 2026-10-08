"""One-command bootstrap against an isolated Compose project and a temp env file.

Never touches the repository's own ``.env``: the run uses ``--env-file`` in a
temporary directory, a unique ``ra-test-*`` Compose project and free ports. Run
it from a checkout that has no ``.env`` of its own (children also read the
repository ``.env`` for keys the temp file leaves empty).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from retail_analytics.bootstrap import local_env
from tests.integration.compose_stack import (
    COMPOSE_FILE,
    ROOT,
    _free_port,
    _require_docker,
)

pytestmark = pytest.mark.docker


@dataclass(frozen=True)
class Run:
    project: str
    env_file: Path
    env: dict[str, str]

    def bootstrap(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "retail_analytics.bootstrap.local_setup",
                "--env-file",
                str(self.env_file),
                "--project",
                self.project,
                "--postgres-port",
                self.env["TEST_PG_PORT"],
                "--temporal-port",
                self.env["TEST_TEMPORAL_PORT"],
            ],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=1500,
        )


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    _require_docker()
    project = "ra-test-" + uuid.uuid4().hex[:8]
    env = {
        **os.environ,
        "RETAIL_ANALYTICS_ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "TEST_PG_PORT": str(_free_port()),
        "TEST_TEMPORAL_PORT": str(_free_port()),
    }
    try:
        yield Run(project, tmp_path / "bootstrap.env", env)
    finally:
        subprocess.run(
            [  # noqa: S607
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                project,
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            capture_output=True,
            check=False,
            timeout=300,
        )


def _counts(env_file: Path) -> tuple[int, int, int]:
    url = local_env.parse_values(env_file.read_text())[local_env.DATABASE_URL_KEY]
    with psycopg.connect(url.replace("+psycopg", "", 1)) as conn:
        cur = conn.cursor()
        counts = []
        for table in ("executives", "product_entitlements", "golden_versions"):
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
            row = cur.fetchone()
            assert row is not None
            counts.append(int(row[0]))
        return counts[0], counts[1], counts[2]


def test_one_command_builds_a_seeded_stack_and_rerun_is_a_noop(run: Run) -> None:
    first = run.bootstrap()
    assert first.returncode == 0, first.stdout + first.stderr

    raw = run.env_file.read_text()
    values = local_env.parse_values(raw)
    secrets_ = [
        values[k]
        for k in (*local_env.GENERATED_SECRETS, *local_env.COMPOSE_VOLUME_PASSWORDS)
    ]
    assert all(len(v) >= 24 for v in secrets_)
    output = first.stdout + first.stderr
    assert all(value not in output for value in secrets_)
    assert "<generated>" in output
    assert "<missing:" in output and "docs/google-access.md" in output
    assert "skipped" in output  # credential check, fixture mode
    assert "exec-demo-a" in output

    executives, entitlements, golden = _counts(run.env_file)
    assert executives == 2 and entitlements > 0 and golden >= 10

    before = run.env_file.read_bytes()
    second = run.bootstrap()
    assert second.returncode == 0, second.stdout + second.stderr
    assert run.env_file.read_bytes() == before
    again = second.stdout + second.stderr
    assert "<generated>" not in again
    assert all(value not in again for value in secrets_)
    assert _counts(run.env_file) == (executives, entitlements, golden)
