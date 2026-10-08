"""Composition of the PostgreSQL persistence adapters behind their ports."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from retail_analytics.adapters.postgres.database import (
    Clock,
    Database,
    IdFactory,
    create_database_engine,
    new_event_id,
    utc_now,
)
from retail_analytics.adapters.postgres.run_events import PostgresRunEventStore
from retail_analytics.adapters.postgres.runs import PostgresRunRepository
from retail_analytics.adapters.postgres.sessions import PostgresSessionRepository
from retail_analytics.adapters.postgres.tool_executions import (
    PostgresQueryJobRepository,
    PostgresToolExecutionRepository,
)
from retail_analytics.application.persistence import (
    QueryJobRepository,
    RunEventStore,
    RunRepository,
    SessionRepository,
    ToolExecutionRepository,
)
from retail_analytics.bootstrap.config import BackendSettings, ConfigError


@dataclass(frozen=True)
class Persistence:
    engine: sa.Engine
    sessions: SessionRepository
    runs: RunRepository
    tool_executions: ToolExecutionRepository
    query_jobs: QueryJobRepository
    run_events: RunEventStore

    def close(self) -> None:
        self.engine.dispose()


def build_persistence(
    database_url: str, *, clock: Clock = utc_now, new_id: IdFactory = new_event_id
) -> Persistence:
    """Create the engine (lazily connecting) and the repositories sharing it."""
    engine = create_database_engine(database_url)
    db = Database(engine, clock=clock, new_id=new_id)
    return Persistence(
        engine=engine,
        sessions=PostgresSessionRepository(db),
        runs=PostgresRunRepository(db),
        tool_executions=PostgresToolExecutionRepository(db),
        query_jobs=PostgresQueryJobRepository(db),
        run_events=PostgresRunEventStore(db),
    )


def persistence_from_settings(settings: BackendSettings) -> Persistence:
    if settings.database_url is None:
        raise ConfigError(["RETAIL_ANALYTICS_DATABASE_URL: required for persistence"])
    return build_persistence(settings.database_url.get_secret_value())
