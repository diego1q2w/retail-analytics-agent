"""Composition of persona management: PostgreSQL versions and delivery to runs."""

from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.adapters.persona.canonical import CanonicalPreviewRenderer
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.persona import PostgresPersonaRepository
from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.persona import Clock, PersonaDelivery, PersonaService
from retail_analytics.application.ports.persona import PreviewRenderer
from retail_analytics.bootstrap.persistence import Persistence


@dataclass(frozen=True, slots=True)
class PersonaServices:
    """``service`` serves editors (CLI, HTTP); ``delivery`` serves runs."""

    service: PersonaService
    delivery: PersonaDelivery


def build_persona(
    persistence: Persistence,
    resolver: AccessResolver,
    *,
    renderer: PreviewRenderer | None = None,
    clock: Clock | None = None,
) -> PersonaServices:
    repository = PostgresPersonaRepository(Database(persistence.engine))
    chosen = renderer or CanonicalPreviewRenderer()
    if clock is None:
        return PersonaServices(
            PersonaService(repository, resolver, chosen), PersonaDelivery(repository)
        )
    return PersonaServices(
        PersonaService(repository, resolver, chosen, clock=clock),
        PersonaDelivery(repository, clock=clock),
    )
