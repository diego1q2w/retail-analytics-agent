"""Composition of context selection and the output privacy gate."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.topic_resets import PostgresTopicResets
from retail_analytics.application.context import ContextBuilder, TopicResets
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.output_privacy import OutputPrivacyGate
from retail_analytics.application.preferences import PreferenceService
from retail_analytics.bootstrap.access import AccessServices
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.domain.context import ContextBudget
from retail_analytics.domain.disclosure import ProtectedTerm


@dataclass(frozen=True)
class ContextServices:
    builder: ContextBuilder
    gate: OutputPrivacyGate
    resets: TopicResets


def build_context(
    persistence: Persistence,
    access: AccessServices,
    evidence: EvidenceService,
    preferences: PreferenceService,
    *,
    budget: ContextBudget | None = None,
    protected_terms: Callable[[], Iterable[ProtectedTerm]] = tuple,
) -> ContextServices:
    """``protected_terms`` supplies trusted literals (e.g. a name lexicon)."""
    resets = PostgresTopicResets(Database(persistence.engine))
    return ContextServices(
        builder=ContextBuilder(
            access.resolver,
            access.guard,
            persistence.sessions,
            evidence,
            preferences,
            resets,
            budget=budget,
            protected_terms=protected_terms,
        ),
        gate=OutputPrivacyGate(
            access.resolver,
            evidence,
            persistence.sessions,
            protected_terms=protected_terms,
        ),
        resets=resets,
    )
