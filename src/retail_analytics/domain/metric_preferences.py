"""Which metric a business term means for one executive.

Precedence is most specific scope first: report, then session, then the
executive's saved default, then the shared catalog default. A preference selects
among definitions; it neither widens data access nor edits an approved formula.
"""

from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.domain.metrics import (
    DEFAULT_REVENUE_METRIC,
    REVENUE_TERM,
    MetricCatalog,
    MetricDefinition,
    MetricDefinitionError,
)
from retail_analytics.domain.periods import OverrideScope

_PRECEDENCE = (OverrideScope.REPORT, OverrideScope.SESSION, OverrideScope.USER_DEFAULT)


@dataclass(frozen=True, slots=True)
class DefinitionPreference:
    """An explicit user choice of meaning, with its scope and provenance."""

    term: str
    metric_id: str
    metric_version: int
    scope: OverrideScope
    provenance: str

    def __post_init__(self) -> None:
        if not self.term or not self.provenance:
            raise MetricDefinitionError("preference needs a term and provenance")


@dataclass(frozen=True, slots=True)
class ResolvedMeaning:
    definition: MetricDefinition
    source: str


def resolve_term(
    catalog: MetricCatalog,
    term: str,
    preferences: tuple[DefinitionPreference, ...] = (),
) -> ResolvedMeaning:
    """Resolve ``term`` (default meaning of 'revenue' is completed-item sales)."""
    for scope in _PRECEDENCE:
        for p in preferences:
            if p.term == term and p.scope is scope:
                return ResolvedMeaning(
                    catalog.get(p.metric_id, p.metric_version),
                    f"{scope.value} preference ({p.provenance})",
                )
    if term == REVENUE_TERM:
        return ResolvedMeaning(catalog.get(DEFAULT_REVENUE_METRIC), "shared default")
    return ResolvedMeaning(catalog.get(term), "catalog metric")
