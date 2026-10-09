"""Approved schema context: what an executive may query, ready for the model.

Ordinary analytical questions need the relation, field, join and metric
definitions every time; without them in context the model rediscovers the
schema with tool calls on every request and follow-up. This service renders
a compact description of exactly the authorized ``CatalogView`` that the
discovery tools return and the SQL compiler validates against, plus the
approved metric definitions those fields support.

Guarantees (the cache is shared by the process, so they are explicit):

- authority is revalidated on every call: the view comes from
  ``DiscoveryService.view_for`` with the caller's freshly resolved
  ``ExecutionContext``; nothing about authority is read from the cache;
- only sanitized approved descriptions are cached: logical names, types,
  reviewed descriptions, joins and metric definitions. Never source tables or
  columns, derivations, product IDs, entitlements or rows;
- entries are segregated by every input that can change what may be shown:
  executive, permissions, product scope (entitlement version and a digest of
  the product set), catalog version, current field/relation availability,
  metadata staleness, metric catalog, render format and size bound. Two
  executives never share an entry, even when their content would match;
- metadata that cannot be validated fails closed: ``CatalogUnavailable``
  yields an UNAVAILABLE context with no schema and drops the executive's
  entries, so nothing earlier is served instead;
- bounded: at most ``max_entries`` entries (least recently used evicted) and
  each description fits ``max_chars`` (descriptions are dropped first, then
  relations, which the discovery tools still describe).
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterable

from retail_analytics.application.contracts.schema_context import (
    SchemaContext,
    SchemaContextStatus,
)
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.discovery import (
    CatalogUnavailable,
    DiscoveryService,
    DiscoveryView,
)
from retail_analytics.domain.catalog import CatalogView, RelationView
from retail_analytics.domain.metrics import (
    DEFAULT_REVENUE_METRIC,
    MetricCatalog,
    MetricDefinition,
    Operation,
    default_catalog,
)

# Bumped when the rendered form changes, so no entry outlives its format.
SCHEMA_CONTEXT_FORMAT = 1
DEFAULT_MAX_CHARS = 6_000
DEFAULT_MAX_ENTRIES = 256

_UNAVAILABLE = SchemaContext(
    SchemaContextStatus.UNAVAILABLE,
    ("Schema information is temporarily unavailable; do not assume any field.",),
    "unavailable",
)


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class ApprovedSchemaContext:
    def __init__(
        self,
        discovery: DiscoveryService,
        metrics: MetricCatalog | None = None,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if max_chars < 200 or max_entries < 1:
            raise ValueError("schema context bounds are too small")
        self._discovery = discovery
        self._metrics = metrics or default_catalog()
        self._metrics_digest = _digest(sorted(d.key for d in self._metrics.definitions))
        self._max_chars = max_chars
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[object, ...], SchemaContext] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._entries)

    async def for_context(
        self, context: ExecutionContext, *, max_chars: int | None = None
    ) -> SchemaContext:
        """The approved schema for this executive now (call on every attempt)."""
        limit = min(self._max_chars, max_chars or self._max_chars)
        try:
            view = await self._discovery.view_for(context)
        except CatalogUnavailable:
            self._forget(context.executive_id)
            return _UNAVAILABLE
        content = _content_digest(view.catalog)
        fingerprint = _digest(
            [view.catalog.catalog_version, content, self._metrics_digest]
        )
        if not view.catalog.relations:
            return SchemaContext(
                SchemaContextStatus.NO_ACCESS,
                ("No data relations are available to this user now.",),
                fingerprint,
                view.metadata_as_of,
                view.stale,
            )
        scope = context.product_scope
        key = (
            context.executive_id,
            tuple(sorted(context.permissions)),
            scope.entitlement_version,
            _digest(sorted(scope.product_ids)),
            view.catalog.catalog_version,
            content,
            view.stale,
            # Staleness is described with its timestamp.
            view.metadata_as_of if view.stale else None,
            self._metrics_digest,
            SCHEMA_CONTEXT_FORMAT,
            limit,
        )
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            return cached
        rendered = self._render(view, fingerprint, limit)
        self._entries[key] = rendered
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return rendered

    def _forget(self, executive_id: str) -> None:
        for key in [k for k in self._entries if k[0] == executive_id]:
            del self._entries[key]

    def _render(
        self, view: DiscoveryView, fingerprint: str, limit: int
    ) -> SchemaContext:
        catalog = view.catalog
        head = [f"catalog v{catalog.catalog_version}"]
        if view.stale:
            head.append(
                "schema metadata could not be refreshed on schedule (as of "
                f"{view.metadata_as_of.isoformat()}); a field may have changed"
            )
        metrics = _metric_lines(self._metrics, catalog)
        lines: list[str] = []
        omitted = 0
        for detailed in (True, False):
            lines = list(head)
            omitted = 0
            relation_lines = [
                _relation_lines(r, detailed=detailed)
                for r in catalog.relations.values()
            ]
            for block in relation_lines:
                if _size(lines) + _size(block) > limit:
                    omitted += 1
                    continue
                lines.extend(block)
            if _size(lines) + _size(metrics) <= limit:
                lines.extend(metrics)
            if not omitted:
                break
        if omitted:
            lines.append(f"{omitted} more relations omitted for space.")
        return SchemaContext(
            SchemaContextStatus.AVAILABLE,
            tuple(lines),
            fingerprint,
            view.metadata_as_of,
            view.stale,
            omitted,
        )


def _size(lines: Iterable[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _content_digest(catalog: CatalogView) -> str:
    """Identity of what the view lets the model use (logical content only)."""
    return _digest(
        [
            [
                r.name,
                r.description,
                r.grain,
                [[f.name, f.type.value, f.description] for f in r.fields],
                [
                    [j.field, j.target, j.target_field, j.cardinality.value]
                    for j in r.joins
                ],
                list(r.unavailable_fields),
            ]
            for r in catalog.relations.values()
        ]
    )


def _relation_lines(relation: RelationView, *, detailed: bool) -> list[str]:
    lines = [f"{relation.name} (row: {relation.grain}): {relation.description}"]
    if detailed:
        fields = "; ".join(
            f"{f.name} ({f.type.value}) {f.description.rstrip('.')}"
            for f in relation.fields
        )
    else:
        fields = ", ".join(f"{f.name} {f.type.value}" for f in relation.fields)
    lines.append(f"  fields: {fields}")
    if relation.joins:
        joins = ", ".join(
            f"{j.field} = {j.target}.{j.target_field} ({j.cardinality.value})"
            for j in relation.joins
        )
        lines.append(f"  joins: {joins}")
    if relation.unavailable_fields:
        lines.append(
            "  temporarily unavailable: " + ", ".join(relation.unavailable_fields)
        )
    return lines


def _metric_lines(metrics: MetricCatalog, catalog: CatalogView) -> list[str]:
    """Approved metrics whose every field is currently usable."""
    usable: dict[str, MetricDefinition] = {}
    latest = [metrics.get(i) for i in sorted(metrics.metric_ids())]
    for metric in latest:
        if metric.operation is Operation.RATIO:
            continue
        relation = catalog.relation(metric.relation.value)
        needed = [metric.time_field, metric.population.status_field]
        needed += [f for f in (metric.measure_field, metric.distinct_field) if f]
        if relation is not None and all(relation.field(f) for f in needed):
            usable[metric.metric_id] = metric
    for metric in latest:
        if metric.operation is Operation.RATIO and all(
            ref in usable for ref in (metric.numerator_id, metric.denominator_id)
        ):
            usable[metric.metric_id] = metric
    if not usable:
        return []
    lines = ["approved metrics:"]
    for metric in usable.values():
        lines.append(f"  {metric.metric_id} v{metric.version} = {_formula(metric)}")
    if DEFAULT_REVENUE_METRIC in usable:
        lines.append(
            f"  revenue means {DEFAULT_REVENUE_METRIC} unless a preference "
            "says otherwise."
        )
    return lines


def _formula(metric: MetricDefinition) -> str:
    statuses = " or ".join(
        f"'{s}'" for s in sorted(metric.population.qualifying_statuses)
    )
    where = (
        f"over {metric.relation.value} where {metric.population.status_field} = "
        f"{statuses}, dated by {metric.time_field}"
    )
    if metric.operation is Operation.SUM:
        expression = f"SUM({metric.measure_field}) {where}"
    elif metric.operation is Operation.COUNT_ROWS:
        expression = f"COUNT(*) {where}"
    elif metric.operation is Operation.COUNT_DISTINCT:
        expression = f"COUNT(DISTINCT {metric.distinct_field}) {where}"
    else:
        expression = f"SAFE_DIVIDE({metric.numerator_id}, {metric.denominator_id})"
    return f"{expression}. {metric.description}"
