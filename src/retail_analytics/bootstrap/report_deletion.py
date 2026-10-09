"""Composition of report deletion: PostgreSQL proposals and the audit trail."""

from __future__ import annotations

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.report_deletion import (
    PostgresReportDeletionRepository,
)
from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.report_deletion import (
    Clock,
    ReportDeletionService,
)
from retail_analytics.bootstrap.persistence import Persistence


def build_report_deletion(
    persistence: Persistence,
    resolver: AccessResolver,
    *,
    clock: Clock | None = None,
) -> ReportDeletionService:
    """The service holds ``propose`` for the model-facing capability
    (``capabilities.report_deletion.report_deletion_capability(service)``) and
    ``preview``/``confirm``/``cancel`` for the authenticated application layer."""
    repository = PostgresReportDeletionRepository(Database(persistence.engine))
    if clock is None:
        return ReportDeletionService(repository, resolver)
    return ReportDeletionService(repository, resolver, clock=clock)
