"""Composition of preference memory on top of persistence and access."""

from __future__ import annotations

from retail_analytics.application.preferences import (
    FindingInvalidator,
    PreferenceService,
)
from retail_analytics.bootstrap.access import AccessServices
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.domain.metrics import MetricCatalog, default_catalog


def build_preferences(
    persistence: Persistence,
    access: AccessServices,
    *,
    catalog: MetricCatalog | None = None,
    invalidator: FindingInvalidator | None = None,
) -> PreferenceService:
    return PreferenceService(
        persistence.preferences,
        access.resolver,
        access.guard,
        catalog or default_catalog(),
        invalidator,
    )
