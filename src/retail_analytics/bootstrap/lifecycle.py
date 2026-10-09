"""Composition of report recovery and lifecycle cleanup."""

from __future__ import annotations

from datetime import timedelta

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.lifecycle import PostgresLifecycleStore
from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.lifecycle import Clock, LifecycleService
from retail_analytics.application.ports.lifecycle import RestoredReportReuse
from retail_analytics.bootstrap.artifacts import ArtifactServices
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.domain.lifecycle import RetentionPolicy


def build_lifecycle(
    persistence: Persistence,
    resolver: AccessResolver,
    artifacts: ArtifactServices,
    *,
    settings: BackendSettings | None = None,
    policy: RetentionPolicy | None = None,
    clock: Clock | None = None,
    reuse: RestoredReportReuse | None = None,
) -> LifecycleService:
    """``restore`` and ``list_restorable`` are for the authenticated application
    layer or the operator CLI; ``run_maintenance`` is the scheduled cleanup.
    None of it is a model capability. ``reuse`` (the ``ReportService``)
    re-validates a restored report's withdrawn reuse links."""
    if policy is None:
        policy = (
            RetentionPolicy()
            if settings is None
            else RetentionPolicy(
                audit_retention=timedelta(days=settings.audit_retention_days),
                batch_size=settings.cleanup_batch_size,
            )
        )
    store = PostgresLifecycleStore(Database(persistence.engine))
    if clock is None:
        return LifecycleService(
            store, resolver, artifacts.maintenance, policy=policy, reuse=reuse
        )
    return LifecycleService(
        store, resolver, artifacts.maintenance, policy=policy, clock=clock, reuse=reuse
    )
