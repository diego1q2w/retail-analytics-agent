"""PostgreSQL support for the local in-process investigation manager.

- ``PostgresManagerLock``: one local manager per database. A session-level
  advisory lock is taken on a dedicated connection and held for the
  manager's lifetime; a second manager fails clearly instead of sharing runs.
  PostgreSQL releases the lock when that connection ends (also when the
  process dies), so a restarted manager can take over. This is not a
  distributed lease: losing the connection silently releases the lock.
- ``PostgresOrphanedLocalRuns``: active local runs left by an ended manager
  and local sessions with queued requests nobody will start.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence

import sqlalchemy as sa

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigation_recovery import (
    sessions_led_by,
)
from retail_analytics.adapters.postgres.schema import run_inputs, runs
from retail_analytics.domain.investigations import InputKind, InputStatus
from retail_analytics.domain.runs import ACTIVE_RUN_STATUSES, ExecutionBackend

_ACTIVE = [status.value for status in ACTIVE_RUN_STATUSES]
# Stable signed 64-bit advisory lock key of the local manager.
LOCAL_MANAGER_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"retail-analytics/local-investigation-manager").digest()[:8],
    "big",
    signed=True,
)


class ManagerLockHeld(Exception):
    """Another local investigation manager already uses this database."""

    def __init__(self) -> None:
        super().__init__(
            "another local investigation manager is running against this "
            "database; stop it first (one API process manages local runs)"
        )


class PostgresManagerLock:
    def __init__(self, engine: sa.Engine, key: int = LOCAL_MANAGER_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._connection: sa.Connection | None = None

    @property
    def held(self) -> bool:
        return self._connection is not None

    async def acquire(self) -> None:
        """Take the lock or raise ``ManagerLockHeld`` (never waits)."""
        if self._connection is not None:
            return
        self._connection = await asyncio.to_thread(self._acquire)

    async def release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await asyncio.to_thread(self._release, connection)

    def _acquire(self) -> sa.Connection:
        connection = self._engine.connect()
        try:
            taken = connection.execute(
                sa.select(sa.func.pg_try_advisory_lock(self._key))
            ).scalar_one()
            connection.commit()
        except BaseException:
            connection.close()
            raise
        if not taken:
            connection.close()
            raise ManagerLockHeld
        return connection

    def _release(self, connection: sa.Connection) -> None:
        try:
            connection.execute(sa.select(sa.func.pg_advisory_unlock(self._key)))
            connection.commit()
        finally:
            # Closing (or a dropped connection) also releases the lock.
            connection.invalidate()
            connection.close()


class PostgresOrphanedLocalRuns:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def active_runs(self, *, owner: str) -> Sequence[str]:
        return await self._db.transaction(self._active, owner)

    @staticmethod
    def _active(connection: sa.Connection, owner: str) -> Sequence[str]:
        return tuple(
            connection.execute(
                sa.select(runs.c.run_id)
                .where(
                    runs.c.execution_backend == ExecutionBackend.LOCAL.value,
                    runs.c.status.in_(_ACTIVE),
                    sa.or_(
                        runs.c.local_execution_id.is_(None),
                        runs.c.local_execution_id != owner,
                    ),
                )
                .order_by(runs.c.created_at, runs.c.run_id)
            ).scalars()
        )

    async def queued_sessions(self) -> Sequence[str]:
        return await self._db.transaction(self._queued)

    @staticmethod
    def _queued(connection: sa.Connection) -> Sequence[str]:
        active = sa.select(runs.c.session_id).where(runs.c.status.in_(_ACTIVE))
        return tuple(
            connection.execute(
                sa.select(run_inputs.c.session_id)
                .distinct()
                .where(
                    run_inputs.c.kind == InputKind.QUEUED.value,
                    run_inputs.c.status == InputStatus.PENDING.value,
                    run_inputs.c.session_id.in_(
                        sessions_led_by(ExecutionBackend.LOCAL)
                    ),
                    run_inputs.c.session_id.not_in(active),
                )
            ).scalars()
        )
