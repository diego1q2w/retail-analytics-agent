"""Development-only identities: ``retail-analytics-dev-access``.

Simulated authentication for local work. ``provision`` creates the local
administrator (the person evaluating the demo: every role plus an explicit
grant of every product) and two restricted synthetic executives with disjoint
product entitlements; ``token`` issues a short-lived locally signed token for
one of them. Both are explicit operator commands that
run with the backend's own settings; there is no API route or flag that skips
authentication. Production identities come from the company identity provider
(see README "Authentication").

The product split follows the public dataset's departments, checked against
``thelook_ecommerce.products`` on 2026-10-08: product IDs 1-15989 are
"Women" and 15990-29120 are "Men".
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

import click

from retail_analytics.application.contracts.authorization import ExecutiveRegistration
from retail_analytics.application.ports.authorization import (
    AccessAdministration,
    ExecutiveDirectory,
)
from retail_analytics.bootstrap.access import local_token_authority
from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    load_backend_settings,
)
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.bootstrap.persistence import (
    Persistence,
    persistence_from_settings,
)
from retail_analytics.domain.access import ExecutiveAccess, Role

# Recorded as the actor of every audited change made by this command (and by
# the bootstrap "executives" step, which runs it).
DEV_ACTOR = "system:dev-access"


@dataclass(frozen=True)
class DemoExecutive:
    key: str
    executive_id: str
    subject: str
    label: str
    roles: frozenset[Role]
    product_ids: frozenset[str]


def _ids(first: int, last: int) -> frozenset[str]:
    return frozenset(str(product_id) for product_id in range(first, last + 1))


# Every product ID in the public dataset (1-29120). Admin roles grant no
# product data, so the local administrator gets this grant explicitly.
ALL_DEMO_PRODUCT_IDS = _ids(1, 29120)

# The local administrator and reviewer of the demo. It is also the only
# identity the local self-publication policy names (``knowledge_admin``).
LOCAL_ADMIN = DemoExecutive(
    key="local-admin",
    executive_id="exec-local-admin",
    subject="local-admin",
    label="Local demo administrator (all products)",
    roles=frozenset({Role.EXECUTIVE, Role.EDITOR, Role.REVIEWER, Role.ADMIN}),
    product_ids=ALL_DEMO_PRODUCT_IDS,
)

# The restricted identities, kept for optional authorization demonstrations.
DEMO_EXECUTIVES: tuple[DemoExecutive, ...] = (
    DemoExecutive(
        key="demo-a",
        executive_id="exec-demo-a",
        subject="demo-executive-a",
        label="Demo executive A (womenswear products)",
        roles=frozenset({Role.EXECUTIVE, Role.EDITOR}),
        product_ids=_ids(1, 15989),
    ),
    DemoExecutive(
        key="demo-b",
        executive_id="exec-demo-b",
        subject="demo-executive-b",
        label="Demo executive B (menswear products)",
        roles=frozenset({Role.EXECUTIVE, Role.REVIEWER}),
        product_ids=_ids(15990, 29120),
    ),
)

# Everything ``provision`` (and the bootstrap "executives" step) creates.
LOCAL_EXECUTIVES: tuple[DemoExecutive, ...] = (LOCAL_ADMIN, *DEMO_EXECUTIVES)


async def provision_demo_executives(
    admin: AccessAdministration,
    issuer: str,
    demos: Sequence[DemoExecutive] = DEMO_EXECUTIVES,
) -> list[ExecutiveAccess]:
    """Create or reconcile the demo executives; safe to rerun."""
    provisioned = []
    for demo in demos:
        await admin.register_executive(
            ExecutiveRegistration(
                executive_id=demo.executive_id,
                issuer=issuer,
                subject=demo.subject,
                roles=demo.roles,
                label=demo.label,
            ),
            actor_id=DEV_ACTOR,
        )
        provisioned.append(
            await admin.replace_products(
                demo.executive_id, demo.product_ids, actor_id=DEV_ACTOR
            )
        )
    return provisioned


def _demo(key: str) -> DemoExecutive:
    for demo in LOCAL_EXECUTIVES:
        if demo.key == key:
            return demo
    raise click.BadParameter(f"unknown demo executive {key!r}")


@click.group()
def main() -> None:
    """Development-only synthetic executives and local tokens."""


@main.command()
def provision() -> None:
    """Create the local admin and the two restricted demo executives."""
    settings = _settings()
    persistence = _persistence(settings)
    try:
        results = asyncio.run(
            provision_demo_executives(
                persistence.access_admin, settings.auth_issuer, LOCAL_EXECUTIVES
            )
        )
    finally:
        persistence.close()
    for access in results:
        roles = ",".join(sorted(access.roles))
        click.echo(
            f"{access.executive_id}: roles={roles} "
            f"products={len(access.product_ids)} "
            f"authorization_version={access.authorization_version}"
        )


@main.command()
@click.argument(
    "executive",
    type=click.Choice([d.key for d in LOCAL_EXECUTIVES]),
    default=LOCAL_ADMIN.key,
)
@click.option(
    "--minutes",
    type=click.IntRange(1, 24 * 60),
    default=60,
    show_default=True,
    help="Token lifetime.",
)
def token(executive: str, minutes: int) -> None:
    """Print a locally signed token for a provisioned demo executive
    (default: the local admin).

    The token goes to stdout only; it is never logged or written to a file.
    Its scopes are the executive's current permissions.
    """
    settings = _settings()
    try:
        authority = local_token_authority(settings)
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    demo = _demo(executive)
    persistence = _persistence(settings)
    try:
        access = asyncio.run(_current(persistence.executives, demo.executive_id))
    finally:
        persistence.close()
    if access is None or not access.active:
        raise click.ClickException(
            f"{demo.executive_id} is not provisioned; run the provision command first"
        )
    click.echo(
        authority.issue(
            demo.subject,
            scopes=access.permissions,
            lifetime=timedelta(minutes=minutes),
        )
    )


async def _current(
    directory: ExecutiveDirectory, executive_id: str
) -> ExecutiveAccess | None:
    return await directory.get(executive_id)


def _settings() -> BackendSettings:
    try:
        return load_backend_settings()
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None


def _persistence(settings: BackendSettings) -> Persistence:
    try:
        return persistence_from_settings(settings)
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None


if __name__ == "__main__":
    main()
