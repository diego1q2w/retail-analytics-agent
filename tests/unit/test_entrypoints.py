"""Smoke tests: every entry point imports and runs offline without credentials."""

from __future__ import annotations

import subprocess
import sys

import click
import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from retail_analytics import __version__
from retail_analytics.bootstrap import api, cli, dev_access, worker
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.domain.access import Role
from retail_analytics.interfaces.cli.app import cli as cli_group


def test_console_scripts_import_in_fresh_interpreter() -> None:
    code = (
        "import retail_analytics.bootstrap.api, retail_analytics.bootstrap.worker,"
        " retail_analytics.bootstrap.cli, retail_analytics.bootstrap.dev_access"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=120)


def test_api_health_in_fixture_mode() -> None:
    client = TestClient(api.build_app(BackendSettings()))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "mode": "fixture",
        "execution_backend": "local",
        "version": __version__,
    }


@pytest.mark.parametrize("command", [api.main, worker.main])
def test_check_config_prints_redacted_summary(command: click.Command) -> None:
    result = CliRunner().invoke(
        command,
        ["--check-config"],
        env={"GEMINI_API_KEY": "sk-hidden"},
    )
    assert result.exit_code == 0, result.output
    assert "APP_MODE=fixture" in result.output
    assert "GEMINI_API_KEY=<set>" in result.output
    assert "sk-hidden" not in result.output


def test_live_mode_without_settings_exits_with_config_error() -> None:
    result = CliRunner().invoke(api.main, ["--check-config"], env={"APP_MODE": "live"})
    assert result.exit_code == 2
    assert "APP_DATABASE_URL" in result.output


def test_worker_exits_promptly_with_local_execution() -> None:
    result = CliRunner().invoke(worker.main, [])
    assert result.exit_code == worker.NOT_USED_EXIT_CODE == 3
    assert "EXECUTION_BACKEND=temporal" in result.output
    assert "retail-analytics-api" in result.output


def test_worker_requires_temporal_configuration() -> None:
    result = CliRunner().invoke(worker.main, [], env={"EXECUTION_BACKEND": "temporal"})
    assert result.exit_code == 2
    assert "TEMPORAL_ADDRESS" in result.output


def test_cli_status_against_backend() -> None:
    backend = TestClient(api.build_app(BackendSettings()))

    def forward(request: httpx.Request) -> httpx.Response:
        reply = backend.get(request.url.path)
        return httpx.Response(reply.status_code, content=reply.content)

    def make_client() -> httpx.Client:
        return httpx.Client(
            base_url="http://backend", transport=httpx.MockTransport(forward)
        )

    result = CliRunner().invoke(cli_group, ["status"], obj=make_client)
    assert result.exit_code == 0, result.output
    assert "backend ok (mode=fixture" in result.output


def test_cli_status_reports_unreachable_backend() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def make_client() -> httpx.Client:
        return httpx.Client(
            base_url="http://backend", transport=httpx.MockTransport(refuse)
        )

    result = CliRunner().invoke(cli_group, ["status"], obj=make_client)
    assert result.exit_code == 1
    assert "backend unavailable (ConnectError)" in result.output


def test_cli_version_and_default_settings() -> None:
    result = CliRunner().invoke(cli_group, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert callable(cli.client_factory())


def test_dev_access_commands_need_explicit_configuration() -> None:
    token = CliRunner().invoke(dev_access.main, ["token", "demo-a"], env={})
    assert token.exit_code == 2
    assert "AUTH_SIGNING_KEY" in token.output

    provision = CliRunner().invoke(dev_access.main, ["provision"], env={})
    assert provision.exit_code == 2
    assert "APP_DATABASE_URL" in provision.output

    unknown = CliRunner().invoke(
        dev_access.main,
        ["token", "someone-else"],
        env={"AUTH_SIGNING_KEY": "k" * 40},
    )
    assert unknown.exit_code == 2


def test_demo_executives_have_disjoint_products_and_no_admin() -> None:
    a, b = dev_access.DEMO_EXECUTIVES
    assert a.product_ids and b.product_ids
    assert not a.product_ids & b.product_ids
    assert all(Role.ADMIN not in demo.roles for demo in (a, b))


def test_api_requires_database_and_signing_key_but_not_temporal() -> None:
    result = CliRunner().invoke(api.main, [], env={})
    assert result.exit_code == 2
    for name in ("APP_DATABASE_URL", "AUTH_SIGNING_KEY"):
        assert name in result.output
    assert "TEMPORAL" not in result.output
    assert "local execution" in result.output


def test_api_with_temporal_execution_also_requires_temporal() -> None:
    result = CliRunner().invoke(api.main, [], env={"EXECUTION_BACKEND": "temporal"})
    assert result.exit_code == 2
    for name in (
        "APP_DATABASE_URL",
        "TEMPORAL_ADDRESS",
        "AUTH_SIGNING_KEY",
    ):
        assert name in result.output


def test_live_mode_requires_the_signing_key() -> None:
    result = CliRunner().invoke(api.main, ["--check-config"], env={"APP_MODE": "live"})
    assert result.exit_code == 2
    assert "AUTH_SIGNING_KEY" in result.output
