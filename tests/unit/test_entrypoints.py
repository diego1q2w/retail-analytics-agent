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
from retail_analytics.bootstrap import api, cli, worker
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.interfaces.cli.app import cli as cli_group


def test_console_scripts_import_in_fresh_interpreter() -> None:
    code = (
        "import retail_analytics.bootstrap.api, retail_analytics.bootstrap.worker,"
        " retail_analytics.bootstrap.cli"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=120)


def test_api_health_in_fixture_mode() -> None:
    client = TestClient(api.build_app(BackendSettings()))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "mode": "fixture",
        "version": __version__,
    }


@pytest.mark.parametrize("command", [api.main, worker.main])
def test_check_config_prints_redacted_summary(command: click.Command) -> None:
    result = CliRunner().invoke(
        command,
        ["--check-config"],
        env={"RETAIL_ANALYTICS_GEMINI_API_KEY": "sk-hidden"},
    )
    assert result.exit_code == 0, result.output
    assert "RETAIL_ANALYTICS_MODE=fixture" in result.output
    assert "RETAIL_ANALYTICS_GEMINI_API_KEY=<set>" in result.output
    assert "sk-hidden" not in result.output


def test_live_mode_without_settings_exits_with_config_error() -> None:
    result = CliRunner().invoke(
        api.main, ["--check-config"], env={"RETAIL_ANALYTICS_MODE": "live"}
    )
    assert result.exit_code == 2
    assert "RETAIL_ANALYTICS_DATABASE_URL" in result.output


def test_worker_exits_cleanly_without_registered_workflows() -> None:
    result = CliRunner().invoke(worker.main, [])
    assert result.exit_code == 0
    assert "no workflows registered" in result.output


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
