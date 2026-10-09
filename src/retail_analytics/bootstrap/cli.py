"""Composition root for the ``analytics`` CLI client."""

from __future__ import annotations

import click
import httpx

from retail_analytics.bootstrap.config import (
    CliSettings,
    ConfigError,
    load_cli_settings,
)
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.interfaces.cli.app import ClientFactory, cli


def _read_token(settings: CliSettings) -> str | None:
    """The bearer token from the token file or the environment, never echoed."""
    if settings.token_file is not None:
        try:
            raw = settings.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            raise ConfigError(
                ["ANALYTICS_CLI_TOKEN_FILE: the file cannot be read"]
            ) from None
        return raw or None
    if settings.token is not None:
        return settings.token.get_secret_value().strip() or None
    return None


def client_factory() -> ClientFactory:
    settings = load_cli_settings()
    token = _read_token(settings)
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    def make_client() -> httpx.Client:
        return httpx.Client(
            base_url=settings.api_url,
            timeout=settings.timeout_seconds,
            headers=headers,
        )

    return make_client


def main() -> None:
    try:
        factory = client_factory()
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    cli(obj=factory)


if __name__ == "__main__":
    main()
