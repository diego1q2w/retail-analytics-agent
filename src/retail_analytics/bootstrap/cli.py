"""Composition root for the ``analytics`` CLI client."""

from __future__ import annotations

import click
import httpx

from retail_analytics.bootstrap.config import ConfigError, load_cli_settings
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.interfaces.cli.app import ClientFactory, cli


def client_factory() -> ClientFactory:
    settings = load_cli_settings()

    def make_client() -> httpx.Client:
        return httpx.Client(base_url=settings.api_url, timeout=settings.timeout_seconds)

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
