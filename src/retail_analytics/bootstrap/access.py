"""Composition of authentication and authorization.

``build_access`` is how entry points (API, worker, dev tools) obtain the
authenticator, the ownership guard and the resolver that produces every
authorized ``ExecutionContext``.
"""

from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.adapters.auth.local_jwt import LocalJwtAuthority
from retail_analytics.adapters.postgres.access_audit import PostgresAccessChangeHistory
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.application.access_audit import AccessAuditService
from retail_analytics.application.authentication import Authenticator
from retail_analytics.application.authorization import AccessResolver, OwnershipGuard
from retail_analytics.application.brand_access import BrandAccessService
from retail_analytics.application.ports.authentication import TokenVerifier
from retail_analytics.application.ports.brand_access import ProductBrandSource
from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    RuntimeMode,
)
from retail_analytics.bootstrap.persistence import Persistence


@dataclass(frozen=True)
class AccessServices:
    authenticator: Authenticator
    guard: OwnershipGuard
    resolver: AccessResolver


def local_token_authority(settings: BackendSettings) -> LocalJwtAuthority:
    if settings.auth_signing_key is None:
        raise ConfigError(["AUTH_SIGNING_KEY: required for token authentication"])
    return LocalJwtAuthority(
        settings.auth_signing_key.get_secret_value(),
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
    )


def build_access(persistence: Persistence, verifier: TokenVerifier) -> AccessServices:
    guard = OwnershipGuard(
        persistence.sessions, persistence.runs, persistence.tool_executions
    )
    return AccessServices(
        authenticator=Authenticator(verifier, persistence.executives),
        guard=guard,
        resolver=AccessResolver(persistence.executives, guard),
    )


def build_access_audit(
    persistence: Persistence, resolver: AccessResolver
) -> AccessAuditService:
    """Admin-only (``access:admin``) history of access changes per executive."""
    return AccessAuditService(
        PostgresAccessChangeHistory(Database(persistence.engine)), resolver
    )


def product_brand_source(settings: BackendSettings) -> ProductBrandSource | None:
    """The trusted ``products.brand`` reader: BigQuery in live mode.

    Fixture mode has no warehouse, so there is no source and the brand
    catalog is not synced (brand-assigned managers then see no products).
    """
    if settings.mode is not RuntimeMode.LIVE or settings.bigquery_project is None:
        return None
    from retail_analytics.adapters.bigquery.product_brands import (
        BigQueryProductBrands,
    )
    from retail_analytics.adapters.google_access import create_bigquery_client

    project, location = settings.bigquery_project, settings.bigquery_location
    return BigQueryProductBrands(
        lambda: create_bigquery_client(project, location), location=location
    )


def build_brand_access(
    persistence: Persistence, source: ProductBrandSource | None = None
) -> BrandAccessService:
    """Operator-only brand assignment and catalog sync (never a model tool)."""
    return BrandAccessService(persistence.brand_access, persistence.executives, source)
