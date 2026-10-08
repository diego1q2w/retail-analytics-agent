"""PostgreSQL ``RunRepository``.

Starting a run locks the session row, so concurrent starts in one session are
serialized: the first wins, a resubmission of the same submission key gets the
same run back, and any other request sees ``ActiveRunExists``. The partial
unique index on active runs backs this up. Different sessions never contend.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import exc

from retail_analytics.adapters.postgres.database import Database, violated_constraint
from retail_analytics.adapters.postgres.schema import messages, runs, sessions
from retail_analytics.adapters.postgres.sessions import (
    insert_message,
    message_from_row,
    session_from_row,
)
from retail_analytics.application.persistence import (
    ActiveRunExists,
    IdempotencyConflict,
    RecordNotFound,
    RunRequest,
    RunStart,
)
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.runs import (
    ACTIVE_RUN_STATUSES,
    Run,
    RunStatus,
    WorkflowRef,
)

_ACTIVE = [status.value for status in ACTIVE_RUN_STATUSES]


def run_from_row(row: sa.Row[tuple[object, ...]]) -> Run:
    m = row._mapping
    workflow_id = m["temporal_workflow_id"]
    return Run(
        run_id=m["run_id"],
        session_id=m["session_id"],
        requested_by=m["requested_by"],
        trigger_message_id=m["trigger_message_id"],
        submission_key=m["submission_key"],
        status=RunStatus(m["status"]),
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        completed_at=m["completed_at"],
        workflow=None
        if workflow_id is None
        else WorkflowRef(workflow_id, m["temporal_run_id"]),
    )


def lock_run(connection: sa.Connection, run_id: str) -> Run:
    row = connection.execute(
        sa.select(runs).where(runs.c.run_id == run_id).with_for_update()
    ).one_or_none()
    if row is None:
        raise RecordNotFound("run", run_id)
    return run_from_row(row)


def transition_locked(
    connection: sa.Connection, run_id: str, to: RunStatus, *, at: datetime
) -> Run:
    """Apply a run transition inside the caller's transaction (row locked)."""
    run = lock_run(connection, run_id)
    updated = run.transition(to, at=at)
    if updated is None:
        return run
    connection.execute(
        sa.update(runs)
        .where(runs.c.run_id == run_id)
        .values(
            status=updated.status.value,
            updated_at=updated.updated_at,
            completed_at=updated.completed_at,
        )
    )
    if updated.status.is_terminal:
        # Retention runs from the later of last interaction and completion.
        connection.execute(
            sa.update(sessions)
            .where(sessions.c.session_id == run.session_id)
            .values(
                last_activity_at=sa.func.greatest(
                    sessions.c.last_activity_at, updated.updated_at
                )
            )
        )
    return updated


class PostgresRunRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def start_run(self, request: RunRequest) -> RunStart:
        return await self._db.transaction(self._start, request)

    def _start(self, connection: sa.Connection, request: RunRequest) -> RunStart:
        session_row = connection.execute(
            sa.select(sessions)
            .where(sessions.c.session_id == request.session_id)
            .with_for_update()
        ).one_or_none()
        # Someone else's session is indistinguishable from a missing one.
        if (
            session_row is None
            or session_from_row(session_row).executive_id != request.requested_by
        ):
            raise RecordNotFound("session", request.session_id)

        earlier = connection.execute(
            sa.select(runs).where(
                runs.c.session_id == request.session_id,
                runs.c.submission_key == request.submission_key,
            )
        ).one_or_none()
        if earlier is not None:
            return self._resubmitted(connection, run_from_row(earlier), request)

        active = connection.execute(
            sa.select(runs.c.run_id).where(
                runs.c.session_id == request.session_id, runs.c.status.in_(_ACTIVE)
            )
        ).scalar_one_or_none()
        if active is not None:
            raise ActiveRunExists(request.session_id, active)

        now = self._db.clock()
        message = insert_message(
            connection,
            message_id=request.message_id,
            session_id=request.session_id,
            role=MessageRole.USER,
            content=request.request_text,
            run_id=None,
            at=now,
        )
        try:
            row = connection.execute(
                sa.insert(runs)
                .values(
                    run_id=request.run_id,
                    session_id=request.session_id,
                    requested_by=request.requested_by,
                    trigger_message_id=request.message_id,
                    submission_key=request.submission_key,
                    status=RunStatus.RUNNING.value,
                    created_at=now,
                    updated_at=now,
                )
                .returning(*runs.c)
            ).one()
        except exc.IntegrityError as error:
            if violated_constraint(error) == "ux_runs_one_active_per_session":
                raise ActiveRunExists(request.session_id, "unknown") from None
            raise IdempotencyConflict("run", request.run_id) from None
        connection.execute(
            sa.update(messages)
            .where(messages.c.message_id == request.message_id)
            .values(run_id=request.run_id)
        )
        return RunStart(
            run=run_from_row(row),
            message=replace(message, run_id=request.run_id),
            created=True,
        )

    @staticmethod
    def _resubmitted(
        connection: sa.Connection, run: Run, request: RunRequest
    ) -> RunStart:
        message = message_from_row(
            connection.execute(
                sa.select(messages).where(
                    messages.c.message_id == run.trigger_message_id
                )
            ).one()
        )
        if message.content != request.request_text:
            raise IdempotencyConflict("run submission", request.submission_key)
        return RunStart(run=run, message=message, created=False)

    async def get_run(self, run_id: str) -> Run | None:
        return await self._db.transaction(self._get, run_id)

    @staticmethod
    def _get(connection: sa.Connection, run_id: str) -> Run | None:
        row = connection.execute(
            sa.select(runs).where(runs.c.run_id == run_id)
        ).one_or_none()
        return None if row is None else run_from_row(row)

    async def active_run(self, session_id: str) -> Run | None:
        return await self._db.transaction(self._active, session_id)

    @staticmethod
    def _active(connection: sa.Connection, session_id: str) -> Run | None:
        row = connection.execute(
            sa.select(runs).where(
                runs.c.session_id == session_id, runs.c.status.in_(_ACTIVE)
            )
        ).one_or_none()
        return None if row is None else run_from_row(row)

    async def transition_run(self, run_id: str, to: RunStatus) -> Run:
        return await self._db.transaction(self._transition, run_id, to)

    def _transition(self, connection: sa.Connection, run_id: str, to: RunStatus) -> Run:
        return transition_locked(connection, run_id, to, at=self._db.clock())

    async def attach_workflow(self, run_id: str, workflow: WorkflowRef) -> Run:
        return await self._db.transaction(self._attach, run_id, workflow)

    def _attach(
        self, connection: sa.Connection, run_id: str, workflow: WorkflowRef
    ) -> Run:
        run = lock_run(connection, run_id)
        current = run.workflow
        if current == workflow:
            return run
        filling_in = (
            current is not None
            and current.workflow_id == workflow.workflow_id
            and current.workflow_run_id is None
        )
        if current is not None and not filling_in:
            raise IdempotencyConflict("run workflow", run_id)
        try:
            row = connection.execute(
                sa.update(runs)
                .where(runs.c.run_id == run_id)
                .values(
                    temporal_workflow_id=workflow.workflow_id,
                    temporal_run_id=workflow.workflow_run_id,
                    updated_at=self._db.clock(),
                )
                .returning(*runs.c)
            ).one()
        except exc.IntegrityError:
            raise IdempotencyConflict("workflow", workflow.workflow_id) from None
        return run_from_row(row)
