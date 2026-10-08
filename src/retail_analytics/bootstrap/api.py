"""Composition root for the HTTP backend (``retail-analytics-api``)."""

from __future__ import annotations

import click
import uvicorn
from fastapi import FastAPI

from retail_analytics import __version__
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.entrypoint import settings_or_exit
from retail_analytics.interfaces.http.app import create_app


def build_app(settings: BackendSettings) -> FastAPI:
    return create_app(mode=settings.mode.value, version=__version__)


@click.command()
@click.option("--check-config", is_flag=True, help="Validate settings and exit.")
def main(check_config: bool) -> None:
    """Run the HTTP backend."""
    settings = settings_or_exit(check_config)
    uvicorn.run(build_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
