"""Composition of the evidence service on top of persistence."""

from __future__ import annotations

import uuid
from datetime import timedelta

from retail_analytics.adapters.postgres.database import Clock, Database, utc_now
from retail_analytics.adapters.postgres.product_scopes import (
    PostgresProductScopeSnapshots,
)
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.domain.evidence import DEFAULT_CURRENT_FRESHNESS, ReusePolicy


def new_evidence_id() -> str:
    return "evd_" + uuid.uuid4().hex


def build_evidence(
    persistence: Persistence,
    *,
    settings: BackendSettings | None = None,
    current_freshness: timedelta | None = None,
    clock: Clock = utc_now,
) -> EvidenceService:
    """Bound automatic reuse for current-data questions.

    Precedence: explicit ``current_freshness`` (tests), then
    ``settings.evidence_current_freshness_seconds``, then the design default.
    """
    if current_freshness is None:
        current_freshness = (
            timedelta(seconds=settings.evidence_current_freshness_seconds)
            if settings is not None
            else DEFAULT_CURRENT_FRESHNESS
        )
    return EvidenceService(
        persistence.evidence,
        persistence.evidence,
        clock=clock,
        new_id=new_evidence_id,
        policy=ReusePolicy(current_freshness),
        # The owner's saved-report evidence may be reused in their other
        # sessions, judged by required-scope coverage (T18-F2).
        imports=persistence.evidence,
        scopes=PostgresProductScopeSnapshots(Database(persistence.engine)),
    )
