"""Persisted investigation intents for the notification recovery dispatcher.

Only Temporal-owned runs are candidates: the dispatcher never starts a
workflow for a run of the local in-process manager (or its queued requests).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import distinct_on

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import (
    messages,
    run_inputs,
    run_principals,
    runs,
)
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.investigations import RecoveryCandidate
from retail_analytics.domain.investigations import InputKind, InputStatus
from retail_analytics.domain.runs import (
    ACTIVE_RUN_STATUSES,
    ExecutionBackend,
    RunStatus,
)


class PostgresRecoveryCandidates:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def active(self) -> Sequence[RecoveryCandidate]:
        return await self._db.transaction(self._active)

    @staticmethod
    def _active(connection: sa.Connection) -> Sequence[RecoveryCandidate]:
        rows = connection.execute(
            sa.select(
                runs.c.run_id,
                runs.c.session_id,
                runs.c.submission_key,
                runs.c.status,
                run_principals.c.executive_id,
                run_principals.c.scopes,
                messages.c.content,
            )
            .select_from(
                runs.join(
                    run_principals, runs.c.run_id == run_principals.c.run_id
                ).join(messages, runs.c.trigger_message_id == messages.c.message_id)
            )
            .where(
                runs.c.status.in_([s.value for s in ACTIVE_RUN_STATUSES]),
                runs.c.execution_backend == ExecutionBackend.TEMPORAL.value,
            )
        ).all()
        return tuple(
            RecoveryCandidate(
                row.run_id,
                row.session_id,
                Principal(row.executive_id, frozenset(row.scopes)),
                row.content,
                row.submission_key,
                RunStatus(row.status),
            )
            for row in rows
        )

    async def queued_sessions(self) -> Sequence[str]:
        return await self._db.transaction(self._queued)

    @staticmethod
    def _queued(connection: sa.Connection) -> Sequence[str]:
        return tuple(
            connection.execute(
                sa.select(run_inputs.c.session_id)
                .distinct()
                .where(
                    run_inputs.c.kind == InputKind.QUEUED.value,
                    run_inputs.c.status == InputStatus.PENDING.value,
                    run_inputs.c.session_id.not_in(
                        sessions_led_by(ExecutionBackend.LOCAL)
                    ),
                )
            ).scalars()
        )


def sessions_led_by(backend: ExecutionBackend) -> sa.Select[tuple[str]]:
    """Sessions whose most recent run belongs to ``backend``.

    A queued request waits behind the session's latest run, so that run's
    backend owns the request; another backend must not start it.
    """
    latest = (
        sa.select(runs.c.session_id, runs.c.execution_backend)
        .ext(distinct_on(runs.c.session_id))
        .order_by(runs.c.session_id, runs.c.created_at.desc(), runs.c.run_id.desc())
        .subquery()
    )
    return sa.select(latest.c.session_id).where(
        latest.c.execution_backend == backend.value
    )
