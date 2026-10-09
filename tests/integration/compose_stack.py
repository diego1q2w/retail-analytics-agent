"""A throwaway Compose project for Docker-marked integration tests.

Each stack gets a unique project name, free loopback ports and random
passwords, and ``running_stack`` removes only that project's containers,
network and volumes afterwards.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.yaml"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def unique_project() -> str:
    """``ra-test-[<RA_TEST_PROJECT_TAG>-]<random>``: the tag lets concurrent
    runs (for example parallel task worktrees) tell their own stacks apart."""
    tag = os.environ.get("RA_TEST_PROJECT_TAG", "")
    return "ra-test-" + (f"{tag}-" if tag else "") + uuid.uuid4().hex[:8]


def _password(role: str) -> str:
    return f"test-{role}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class Stack:
    project: str = field(default_factory=unique_project)
    postgres_port: int = field(default_factory=_free_port)
    temporal_port: int = field(default_factory=_free_port)
    app_password: str = field(default_factory=lambda: _password("app"))
    temporal_password: str = field(default_factory=lambda: _password("temporal"))
    admin_password: str = field(default_factory=lambda: _password("admin"))

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project, *args],  # noqa: S607
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker compose {args} failed:\n{result.stderr}")
        return result

    @property
    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "COMPOSE_POSTGRES_PORT": str(self.postgres_port),
            "COMPOSE_TEMPORAL_PORT": str(self.temporal_port),
            "COMPOSE_APP_DB_PASSWORD": self.app_password,
            "COMPOSE_TEMPORAL_DB_PASSWORD": self.temporal_password,
            "COMPOSE_PG_ADMIN_PASSWORD": self.admin_password,
        }

    def dsn(self, user: str, password: str, database: str) -> str:
        return (
            f"host=127.0.0.1 port={self.postgres_port} user={user} "
            f"password={password} dbname={database} connect_timeout=5"
        )

    @property
    def app_dsn(self) -> str:
        return self.dsn("retail_app", self.app_password, "retail_app")

    @property
    def app_url(self) -> str:
        return (
            f"postgresql+psycopg://retail_app:{self.app_password}"
            f"@127.0.0.1:{self.postgres_port}/retail_app"
        )

    def migrate(self, target: str = "head") -> subprocess.CompletedProcess[str]:
        return self.alembic("upgrade", target)

    def alembic(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "APP_DATABASE_URL": self.app_url}
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    probe = subprocess.run(
        ["docker", "info"],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip("docker daemon is not reachable")


@contextmanager
def running_stack(*services: str) -> Iterator[Stack]:
    """Start ``services`` (and their dependencies) and tear everything down."""
    _require_docker()
    stack = Stack()
    try:
        stack.compose("up", "-d", "--wait", *services)
        yield stack
    finally:
        subprocess.run(
            [  # noqa: S607
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                stack.project,
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            env=stack.env,
            capture_output=True,
            check=False,
            timeout=300,
        )
