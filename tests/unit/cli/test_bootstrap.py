from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.bootstrap import cli as bootstrap_cli
from retail_analytics.bootstrap.config import ConfigError, load_cli_settings


def test_token_comes_from_file_or_environment_and_is_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token-123\n")
    monkeypatch.setenv("ANALYTICS_CLI_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ANALYTICS_CLI_TOKEN", "env-token-456")
    client = bootstrap_cli.client_factory()()
    assert client.headers["authorization"] == "Bearer file-token-123"

    monkeypatch.delenv("ANALYTICS_CLI_TOKEN_FILE")
    assert (
        bootstrap_cli.client_factory()().headers["authorization"]
        == "Bearer env-token-456"
    )
    settings = load_cli_settings({"ANALYTICS_CLI_TOKEN": "env-token-456"}, None)
    assert "env-token-456" not in repr(settings)

    monkeypatch.delenv("ANALYTICS_CLI_TOKEN")
    assert "authorization" not in bootstrap_cli.client_factory()().headers


def test_unreadable_token_file_is_a_config_error_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANALYTICS_CLI_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(ConfigError) as caught:
        bootstrap_cli.client_factory()
    assert "TOKEN_FILE" in str(caught.value)
    with pytest.raises(SystemExit) as exit_info:
        bootstrap_cli.main()
    assert exit_info.value.code == 2
