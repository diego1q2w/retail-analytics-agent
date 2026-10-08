"""Shared start-up handling for backend entry points."""

from __future__ import annotations

import click

from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    load_backend_settings,
)

CONFIG_ERROR_EXIT_CODE = 2


def settings_or_exit(check_only: bool) -> BackendSettings:
    """Load settings. Exit 2 if invalid; with ``check_only`` print and exit 0."""
    try:
        settings = load_backend_settings()
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    if check_only:
        for name, value in settings.redacted_summary().items():
            click.echo(f"{name}={value}")
        raise SystemExit(0)
    return settings
