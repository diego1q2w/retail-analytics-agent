"""Composition of the evidence service on top of persistence."""

from __future__ import annotations

import uuid
from datetime import timedelta

from retail_analytics.adapters.postgres.database import Clock, utc_now
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.domain.evidence import DEFAULT_CURRENT_FRESHNESS, ReusePolicy


def new_evidence_id() -> str:
    return "evd_" + uuid.uuid4().hex


def build_evidence(
    persistence: Persistence,
    *,
    current_freshness: timedelta = DEFAULT_CURRENT_FRESHNESS,
    clock: Clock = utc_now,
) -> EvidenceService:
    """``current_freshness`` bounds automatic reuse for current-data questions."""
    return EvidenceService(
        persistence.evidence,
        persistence.evidence,
        clock=clock,
        new_id=new_evidence_id,
        policy=ReusePolicy(current_freshness),
    )
