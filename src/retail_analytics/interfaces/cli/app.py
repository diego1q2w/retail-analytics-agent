"""``analytics`` command group. Talks to the backend only through HTTP.

Bootstrap passes a client factory in ``ctx.obj`` so commands never build their
own transport or read configuration.
"""

from __future__ import annotations

from collections.abc import Callable

import click
import httpx

ClientFactory = Callable[[], httpx.Client]


@click.group()
@click.version_option(package_name="retail-analytics-agent", prog_name="analytics")
def cli() -> None:
    """Retail analytics assistant."""


@cli.command()
@click.pass_obj
def status(make_client: ClientFactory) -> None:
    """Check that the backend is reachable and show its mode."""
    try:
        with make_client() as client:
            response = client.get("/healthz")
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"backend unavailable ({type(exc).__name__})"
        ) from None
    click.echo(
        f"backend {body['status']} (mode={body['mode']}, version={body['version']})"
    )
