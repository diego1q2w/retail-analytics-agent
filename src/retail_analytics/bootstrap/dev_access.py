"""Development-only identities: ``retail-analytics-dev-access``.

Simulated authentication for local work. ``provision`` creates the local
administrator (the person evaluating the demo: every role plus an explicit
grant of every product) and two restricted synthetic brand managers with
disjoint assigned brands; ``token`` issues a short-lived locally signed token
for one of them. ``sync-brands`` reads ``products.brand`` from the warehouse
(live mode) into the trusted snapshot the brands resolve through, and
``brands list|assign|remove`` changes a manager's assigned brands. All are
explicit operator commands that run with the backend's own settings; there is
no API route or flag that skips authentication, and no model tool changes
access. Production identities come from the company identity provider (see
README "Authentication"); production administration of brand assignments is
an open question (docs/brand-access.md).

Demo brands were checked against ``thelook_ecommerce.products`` on
2026-10-09: "Calvin Klein" (497 products) and "Levi's" (259) for manager A,
"Carhartt" (388) and "Columbia" (236) for manager B. None has a case
variant; related labels such as "Calvin Klein Jeans" are separate catalog
brands and are not included (exact match).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import click

from retail_analytics.application.brand_access import (
    BrandAccessError,
    BrandAccessService,
    ProductBrandsUnavailable,
)
from retail_analytics.application.contracts.authorization import ExecutiveRegistration
from retail_analytics.application.contracts.brand_access import BrandAssignment
from retail_analytics.application.contracts.persistence import RecordNotFound
from retail_analytics.application.ports.authorization import (
    AccessAdministration,
    ExecutiveDirectory,
)
from retail_analytics.application.ports.brand_access import BrandAccessStore
from retail_analytics.bootstrap.access import (
    build_brand_access,
    local_token_authority,
    product_brand_source,
)
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
    # Explicit product grants (the local admin's full grant).
    product_ids: frozenset[str]
    # Assigned brands, resolved to products through the synced catalog.
    brands: frozenset[str] = frozenset()


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

# The restricted identities: brand managers for optional authorization
# demonstrations. They have no explicit product grants (provisioning removes
# the department ranges older versions granted); their brands are their scope.
DEMO_EXECUTIVES: tuple[DemoExecutive, ...] = (
    DemoExecutive(
        key="demo-a",
        executive_id="exec-demo-a",
        subject="demo-executive-a",
        label="Demo manager A (Calvin Klein, Levi's)",
        roles=frozenset({Role.EXECUTIVE, Role.EDITOR}),
        product_ids=frozenset(),
        brands=frozenset({"Calvin Klein", "Levi's"}),
    ),
    DemoExecutive(
        key="demo-b",
        executive_id="exec-demo-b",
        subject="demo-executive-b",
        label="Demo manager B (Carhartt, Columbia)",
        roles=frozenset({Role.EXECUTIVE, Role.REVIEWER}),
        product_ids=frozenset(),
        brands=frozenset({"Carhartt", "Columbia"}),
    ),
)

# Everything ``provision`` (and the bootstrap "executives" step) creates.
LOCAL_EXECUTIVES: tuple[DemoExecutive, ...] = (LOCAL_ADMIN, *DEMO_EXECUTIVES)


async def provision_demo_executives(
    admin: AccessAdministration,
    issuer: str,
    demos: Sequence[DemoExecutive] = DEMO_EXECUTIVES,
    *,
    brands: BrandAccessStore | None = None,
) -> list[ExecutiveAccess]:
    """Create or reconcile the demo executives; safe to rerun.

    Explicit grants and assigned brands are both set to the declared values.
    Brands are stored even before the catalog is synced (they grant nothing
    until a sync finds their products).
    """
    if brands is None and any(demo.brands for demo in demos):
        raise ValueError("demo brand managers need the brand access store")
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
        access = await admin.replace_products(
            demo.executive_id, demo.product_ids, actor_id=DEV_ACTOR
        )
        if brands is not None:
            access = await brands.replace_brands(
                demo.executive_id, demo.brands, actor_id=DEV_ACTOR
            )
        provisioned.append(access)
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
    """Create the local admin and the two restricted demo brand managers."""
    settings = _settings()
    persistence = _persistence(settings)
    try:
        results = asyncio.run(
            provision_demo_executives(
                persistence.access_admin,
                settings.auth_issuer,
                LOCAL_EXECUTIVES,
                brands=persistence.brand_access,
            )
        )
    finally:
        persistence.close()
    for demo, access in zip(LOCAL_EXECUTIVES, results, strict=True):
        roles = ",".join(sorted(access.roles))
        brands = f" brands={_brand_list(demo.brands)}" if demo.brands else ""
        click.echo(
            f"{access.executive_id}: roles={roles} "
            f"products={len(access.product_ids)}{brands} "
            f"authorization_version={access.authorization_version}"
        )
    pairs = zip(LOCAL_EXECUTIVES, results, strict=True)
    if any(demo.brands and not access.product_ids for demo, access in pairs):
        click.echo(
            "Brand managers see no products until the brand catalog is synced "
            "(retail-analytics-dev-access sync-brands, live mode)."
        )


def _brand_list(brands: Iterable[str]) -> str:
    return ",".join(sorted(brands))


def _executive_id(executive: str) -> str:
    """A demo key (``demo-a``) or any executive ID."""
    for demo in LOCAL_EXECUTIVES:
        if demo.key == executive:
            return demo.executive_id
    return executive


def _show(assignment: BrandAssignment) -> None:
    access = assignment.access
    click.echo(
        f"{access.executive_id}: products={len(access.product_ids)} "
        f"authorization_version={access.authorization_version}"
    )
    for brand, count in assignment.brand_products.items():
        note = "" if count else " (matches no catalog products: grants nothing)"
        click.echo(f"  {brand}: {count} products{note}")


def _run_brands(
    action: Callable[[BrandAccessService], Coroutine[Any, Any, BrandAssignment]],
) -> None:
    settings = _settings()
    persistence = _persistence(settings)
    try:
        assignment = asyncio.run(action(build_brand_access(persistence)))
    except RecordNotFound:
        raise click.ClickException("unknown executive") from None
    except BrandAccessError as exc:
        raise click.ClickException(_brand_error(exc)) from None
    finally:
        persistence.close()
    _show(assignment)


def _brand_error(exc: BrandAccessError) -> str:
    names = ", ".join(exc.brands)
    if exc.code == "unknown_brand":
        return (
            f"not in the synced brand catalog (exact match): {names}. "
            "Check the spelling, run sync-brands, or pass --allow-unmatched "
            "to store it anyway (it grants nothing until it matches)."
        )
    if exc.code == "invalid_brand":
        return f"invalid brand name: {names}"
    return exc.code


@main.group()
def brands() -> None:
    """Assigned brands of a manager (local administration).

    EXECUTIVE is a demo key (demo-a, demo-b, local-admin) or an executive ID.
    Each change bumps the executive's authorization version and is audited.
    """


@brands.command("list")
@click.argument("executive")
def list_brands(executive: str) -> None:
    """Show the assigned brands and how many products each resolves to."""
    executive_id = _executive_id(executive)
    _run_brands(lambda service: service.assignment(executive_id))


@brands.command()
@click.argument("executive")
@click.argument("brand_names", metavar="BRAND...", nargs=-1, required=True)
@click.option(
    "--allow-unmatched",
    is_flag=True,
    help="Store brands the synced catalog does not contain (they grant nothing).",
)
def assign(executive: str, brand_names: tuple[str, ...], allow_unmatched: bool) -> None:
    """Add brands (exact catalog spelling) to a manager."""
    executive_id = _executive_id(executive)
    _run_brands(
        lambda service: service.assign(
            executive_id,
            brand_names,
            actor_id=DEV_ACTOR,
            allow_unmatched=allow_unmatched,
        )
    )


@brands.command()
@click.argument("executive")
@click.argument("brand_names", metavar="BRAND...", nargs=-1, required=True)
def remove(executive: str, brand_names: tuple[str, ...]) -> None:
    """Remove brands from a manager (access narrows immediately)."""
    executive_id = _executive_id(executive)
    _run_brands(
        lambda service: service.remove(executive_id, brand_names, actor_id=DEV_ACTOR)
    )


@main.command("sync-brands")
def sync_brands() -> None:
    """Read products.brand from the warehouse into the trusted snapshot.

    Live mode only (BigQuery). Executives whose brands now resolve to other
    products get a new authorization version, so cached evidence, schema
    context and report access are re-checked against the new scope.
    """
    settings = _settings()
    source = product_brand_source(settings)
    if source is None:
        raise click.ClickException(
            "no brand catalog source: needs live mode with BIGQUERY_PROJECT"
        )
    persistence = _persistence(settings)
    try:
        result = asyncio.run(
            build_brand_access(persistence, source).sync_catalog(actor_id=DEV_ACTOR)
        )
    except (ProductBrandsUnavailable, BrandAccessError) as exc:
        raise click.ClickException(f"brand catalog not synced: {exc}") from None
    finally:
        persistence.close()
    click.echo(
        f"brand catalog {result.catalog_digest}: products={result.products} "
        f"brands={result.brands} without_brand={result.products_without_brand} "
        f"changed={result.products_changed}"
    )
    for change in result.executives_changed:
        click.echo(
            f"{change.executive_id}: products {change.products_before} -> "
            f"{change.products_after} authorization_version="
            f"{change.new_authorization_version}"
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
