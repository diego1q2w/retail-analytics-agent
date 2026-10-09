"""Composition of saved reports: PostgreSQL metadata, artifacts and the output gate."""

from __future__ import annotations

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.product_scopes import (
    PostgresProductScopeSnapshots,
)
from retail_analytics.adapters.postgres.reports import PostgresReportRepository
from retail_analytics.application.artifacts import ArtifactService
from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.output_privacy import OutputPrivacyGate
from retail_analytics.application.reports import ReportService
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.domain.metrics import MetricCatalog, default_catalog


def build_reports(
    persistence: Persistence,
    artifacts: ArtifactService,
    evidence: EvidenceService,
    gate: OutputPrivacyGate,
    resolver: AccessResolver,
    *,
    metrics: MetricCatalog | None = None,
    declared_currency: str | None = None,
) -> ReportService:
    """``gate`` is ``build_context(...).gate``; ``artifacts`` is
    ``build_artifacts(...).service``."""
    db = Database(persistence.engine)
    return ReportService(
        PostgresReportRepository(db),
        artifacts,
        evidence,
        gate,
        resolver,
        metrics or default_catalog(),
        PostgresProductScopeSnapshots(db),
        persistence.preferences,
        declared_currency=declared_currency,
    )
