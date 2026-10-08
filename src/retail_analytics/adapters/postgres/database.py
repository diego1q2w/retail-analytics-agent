"""Shared plumbing for the PostgreSQL adapters.

The ports are async; the adapters use a synchronous SQLAlchemy Core engine
(psycopg 3) and run each unit of work in a worker thread, one transaction per
call. Row locks (``SELECT ... FOR UPDATE``) serialize competing writers; unique
constraints are the final guard and are mapped to typed errors by name.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Concatenate

import sqlalchemy as sa
from sqlalchemy import exc

type Clock = Callable[[], datetime]
type IdFactory = Callable[[], str]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_event_id() -> str:
    return uuid.uuid4().hex


def create_database_engine(url: str) -> sa.Engine:
    return sa.create_engine(url, pool_pre_ping=True)


class Database:
    def __init__(
        self,
        engine: sa.Engine,
        *,
        clock: Clock = utc_now,
        new_id: IdFactory = new_event_id,
    ) -> None:
        self.engine = engine
        self.clock = clock
        self.new_id = new_id

    async def transaction[**P, R](
        self,
        work: Callable[Concatenate[sa.Connection, P], R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Run ``work(connection, ...)`` in one transaction off the event loop."""

        def run() -> R:
            with self.engine.begin() as connection:
                return work(connection, *args, **kwargs)

        return await asyncio.to_thread(run)


def violated_constraint(error: exc.IntegrityError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None
