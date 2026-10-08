"""Authorized schema discovery over a cached, drift-checked source schema.

Two things are deliberately separate:

- the *source schema* (shared, cached, refreshed on an interval or on demand):
  what the warehouse currently has, checked against the reviewed catalog;
- the *executive's view* (never cached): computed from the current
  ``ExecutionContext`` on every call, so an entitlement change takes effect on
  the next call even while the source schema is warm.

Errors never echo raw warehouse names. Drift disables only the affected
logical fields; an unreachable warehouse fails closed once the last good
snapshot is too old.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.ports.discovery import SourceMetadataProvider
from retail_analytics.domain.access import Permission
from retail_analytics.domain.catalog import (
    CatalogHealth,
    CatalogView,
    LogicalCatalog,
    RelationView,
    SourceSchema,
    build_view,
    evaluate_health,
)

DEFAULT_REFRESH_INTERVAL = timedelta(hours=1)
DEFAULT_MAX_STALE = timedelta(hours=24)
DEFAULT_RETRY_BACKOFF = timedelta(seconds=30)


class SourceMetadataUnavailable(Exception):
    """The warehouse metadata could not be read. Carries no provider detail."""


class CatalogUnavailable(Exception):
    """No sufficiently fresh, validated source schema is available."""


class RelationNotAvailable(Exception):
    """Unknown, forbidden or disabled relation (deliberately indistinguishable)."""


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    fetched_at: datetime
    schema: SourceSchema
    health: CatalogHealth


class SourceSchemaCache:
    """Shared cache of the validated source schema.

    Holds source metadata only: never rows, never per-executive data.
    """

    def __init__(
        self,
        catalog: LogicalCatalog,
        provider: SourceMetadataProvider,
        clock: Callable[[], datetime],
        *,
        refresh_interval: timedelta = DEFAULT_REFRESH_INTERVAL,
        max_stale: timedelta = DEFAULT_MAX_STALE,
        retry_backoff: timedelta = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        if refresh_interval <= timedelta(0) or max_stale < refresh_interval:
            raise ValueError("need 0 < refresh_interval <= max_stale")
        self._catalog = catalog
        self._provider = provider
        self._clock = clock
        self._interval = refresh_interval
        self._max_stale = max_stale
        self._backoff = retry_backoff
        self._snapshot: SchemaSnapshot | None = None
        self._invalidated = False
        self._last_failure: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> SchemaSnapshot | None:
        return self._snapshot

    def invalidate(self) -> None:
        """Force revalidation on next use, e.g. after a schema mismatch error."""
        self._invalidated = True

    async def current(self) -> SchemaSnapshot:
        """A usable snapshot, refreshing when due. Raises CatalogUnavailable."""
        if self._is_fresh():
            return self._require()
        async with self._lock:
            if self._is_fresh():
                return self._require()
            now = self._clock()
            if not self._in_backoff(now):
                try:
                    await self._refresh(now)
                    return self._require()
                except SourceMetadataUnavailable:
                    self._last_failure = now
            return self._stale_or_fail(now)

    def _is_fresh(self) -> bool:
        snap = self._snapshot
        return (
            snap is not None
            and not self._invalidated
            and self._clock() - snap.fetched_at < self._interval
        )

    def _in_backoff(self, now: datetime) -> bool:
        last = self._last_failure
        return last is not None and now - last < self._backoff

    async def _refresh(self, now: datetime) -> None:
        schema = await self._provider.read_schema(self._catalog.source_tables)
        health = evaluate_health(self._catalog, schema)
        self._snapshot = SchemaSnapshot(now, schema, health)
        self._invalidated = False
        self._last_failure = None

    def _stale_or_fail(self, now: datetime) -> SchemaSnapshot:
        snap = self._snapshot
        if snap is not None and now - snap.fetched_at <= self._max_stale:
            return snap
        raise CatalogUnavailable("schema metadata is temporarily unavailable")

    def _require(self) -> SchemaSnapshot:
        if self._snapshot is None:
            raise CatalogUnavailable("schema metadata is temporarily unavailable")
        return self._snapshot


@dataclass(frozen=True, slots=True)
class DiscoveryView:
    """An authorized view plus the freshness of the metadata behind it."""

    catalog: CatalogView
    metadata_as_of: datetime
    stale: bool


class DiscoveryService:
    """Produces the one authorized catalog view for discovery and compilation."""

    def __init__(
        self,
        catalog: LogicalCatalog,
        cache: SourceSchemaCache,
        clock: Callable[[], datetime],
        *,
        stale_after: timedelta = DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        self._catalog = catalog
        self._cache = cache
        self._clock = clock
        self._stale_after = stale_after

    async def view_for(self, context: ExecutionContext) -> DiscoveryView:
        """The relations and fields this executive may use right now.

        Call with a context freshly resolved for this attempt; nothing derived
        from the context is cached.
        """
        snapshot = await self._cache.current()
        allowed = (
            Permission.ANALYSIS_READ in context.permissions
            and not context.product_scope.is_empty
        )
        view = build_view(
            self._catalog,
            snapshot.health,
            entitlement_version=context.product_scope.entitlement_version,
            visible=allowed,
        )
        stale = self._clock() - snapshot.fetched_at >= self._stale_after
        return DiscoveryView(view, snapshot.fetched_at, stale)

    async def list_relations(self, context: ExecutionContext) -> DiscoveryView:
        return await self.view_for(context)

    async def describe_relation(
        self, context: ExecutionContext, name: str
    ) -> tuple[DiscoveryView, RelationView]:
        discovery = await self.view_for(context)
        relation = discovery.catalog.relation(name)
        if relation is None:
            raise RelationNotAvailable(name[:64])
        return discovery, relation
