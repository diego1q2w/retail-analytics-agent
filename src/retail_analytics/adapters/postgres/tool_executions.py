"""PostgreSQL ``ToolExecutionRepository`` and ``QueryJobRepository``.

``begin`` inserts with ``ON CONFLICT DO NOTHING`` on the operation ID: a
concurrent duplicate waits for the first insert to commit and then reads that
record, so one operation key yields exactly one execution. Transitions lock
the operation row, apply the domain rule, update current state and append the
history entry in the same transaction.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import (
    execution_events,
    query_executions,
    runs,
    tool_executions,
)
from retail_analytics.application.persistence import (
    IdempotencyConflict,
    OperationRequest,
    OperationStart,
    RecordNotFound,
)
from retail_analytics.domain.executions import (
    ExecutionEvent,
    QueryJob,
    ToolExecution,
    ToolExecutionStatus,
)
from retail_analytics.domain.operations import SideEffect, ToolErrorCode


def _execution(row: sa.Row[tuple[object, ...]]) -> ToolExecution:
    m = row._mapping
    return ToolExecution(
        operation_id=m["operation_id"],
        run_id=m["run_id"],
        capability=m["capability"],
        capability_version=m["capability_version"],
        side_effect=SideEffect(m["side_effect"]),
        status=ToolExecutionStatus(m["status"]),
        attempt_count=m["attempt_count"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        deadline_at=m["deadline_at"],
        error_code=None if m["error_code"] is None else ToolErrorCode(m["error_code"]),
        error_detail=m["error_detail"],
    )


def _event(row: sa.Row[tuple[object, ...]]) -> ExecutionEvent:
    m = row._mapping
    return ExecutionEvent(
        operation_id=m["operation_id"],
        sequence=m["sequence"],
        from_status=None
        if m["from_status"] is None
        else ToolExecutionStatus(m["from_status"]),
        to_status=ToolExecutionStatus(m["to_status"]),
        attempt=m["attempt"],
        occurred_at=m["occurred_at"],
        error_code=None if m["error_code"] is None else ToolErrorCode(m["error_code"]),
        detail=m["detail"],
    )


def _job(row: sa.Row[tuple[object, ...]]) -> QueryJob:
    m = row._mapping
    return QueryJob(
        operation_id=m["operation_id"],
        job_id=m["job_id"],
        project=m["project"],
        location=m["location"],
        query_fingerprint=m["query_fingerprint"],
        query_ref=m["query_ref"],
        authorization_version=m["authorization_version"],
        catalog_version=m["catalog_version"],
        submission=m["submission"],
    )


def _find_execution(
    connection: sa.Connection, operation_id: str
) -> ToolExecution | None:
    row = connection.execute(
        sa.select(tool_executions).where(tool_executions.c.operation_id == operation_id)
    ).one_or_none()
    return None if row is None else _execution(row)


def _same_request(execution: ToolExecution, request: OperationRequest) -> bool:
    return (
        execution.run_id,
        execution.capability,
        execution.capability_version,
        execution.side_effect,
    ) == (
        request.run_id,
        request.capability,
        request.capability_version,
        request.side_effect,
    )


class PostgresToolExecutionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def begin(self, request: OperationRequest) -> OperationStart:
        return await self._db.transaction(self._begin, request)

    def _begin(
        self, connection: sa.Connection, request: OperationRequest
    ) -> OperationStart:
        run_exists = connection.execute(
            sa.select(runs.c.run_id).where(runs.c.run_id == request.run_id)
        ).one_or_none()
        if run_exists is None:
            raise RecordNotFound("run", request.run_id)
        now = self._db.clock()
        row = connection.execute(
            insert(tool_executions)
            .values(
                operation_id=request.operation_id,
                run_id=request.run_id,
                capability=request.capability,
                capability_version=request.capability_version,
                side_effect=request.side_effect.value,
                status=ToolExecutionStatus.PREPARED.value,
                attempt_count=0,
                deadline_at=request.deadline_at,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["operation_id"])
            .returning(*tool_executions.c)
        ).one_or_none()
        if row is None:
            existing = _find_execution(connection, request.operation_id)
            if existing is None or not _same_request(existing, request):
                raise IdempotencyConflict("operation", request.operation_id)
            return OperationStart(execution=existing, created=False)
        connection.execute(
            sa.insert(execution_events).values(
                operation_id=request.operation_id,
                sequence=1,
                from_status=None,
                to_status=ToolExecutionStatus.PREPARED.value,
                attempt=0,
                occurred_at=now,
            )
        )
        return OperationStart(execution=_execution(row), created=True)

    async def get(self, operation_id: str) -> ToolExecution | None:
        return await self._db.transaction(_find_execution, operation_id)

    async def transition(
        self,
        operation_id: str,
        to: ToolExecutionStatus,
        *,
        attempt: int,
        error_code: ToolErrorCode | None = None,
        detail: str | None = None,
    ) -> ToolExecution:
        return await self._db.transaction(
            self._transition, operation_id, to, attempt, error_code, detail
        )

    def _transition(
        self,
        connection: sa.Connection,
        operation_id: str,
        to: ToolExecutionStatus,
        attempt: int,
        error_code: ToolErrorCode | None,
        detail: str | None,
    ) -> ToolExecution:
        row = connection.execute(
            sa.select(tool_executions)
            .where(tool_executions.c.operation_id == operation_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise RecordNotFound("operation", operation_id)
        current = _execution(row)
        updated = current.transition(
            to,
            attempt=attempt,
            at=self._db.clock(),
            error_code=error_code,
            detail=detail,
        )
        if updated is None:
            return current
        connection.execute(
            sa.update(tool_executions)
            .where(tool_executions.c.operation_id == operation_id)
            .values(
                status=updated.status.value,
                attempt_count=updated.attempt_count,
                error_code=None
                if updated.error_code is None
                else updated.error_code.value,
                error_detail=updated.error_detail,
                updated_at=updated.updated_at,
            )
        )
        last = connection.execute(
            sa.select(sa.func.max(execution_events.c.sequence)).where(
                execution_events.c.operation_id == operation_id
            )
        ).scalar_one()
        connection.execute(
            sa.insert(execution_events).values(
                operation_id=operation_id,
                sequence=(last or 0) + 1,
                from_status=current.status.value,
                to_status=updated.status.value,
                attempt=attempt,
                error_code=None if error_code is None else error_code.value,
                detail=detail,
                occurred_at=updated.updated_at,
            )
        )
        return updated

    async def history(self, operation_id: str) -> Sequence[ExecutionEvent]:
        return await self._db.transaction(self._history, operation_id)

    @staticmethod
    def _history(connection: sa.Connection, operation_id: str) -> list[ExecutionEvent]:
        rows = connection.execute(
            sa.select(execution_events)
            .where(execution_events.c.operation_id == operation_id)
            .order_by(execution_events.c.sequence)
        ).all()
        return [_event(row) for row in rows]

    async def for_run(self, run_id: str) -> Sequence[ToolExecution]:
        return await self._db.transaction(self._for_run, run_id)

    @staticmethod
    def _for_run(connection: sa.Connection, run_id: str) -> list[ToolExecution]:
        rows = connection.execute(
            sa.select(tool_executions)
            .where(tool_executions.c.run_id == run_id)
            .order_by(tool_executions.c.created_at, tool_executions.c.operation_id)
        ).all()
        return [_execution(row) for row in rows]


class PostgresQueryJobRepository:
    """Job references per operation, one per submission, never overwritten.

    A new submission must follow the latest one (1, 2, ...); registering an
    existing submission again with identical content returns it.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def register_job(self, job: QueryJob) -> QueryJob:
        return await self._db.transaction(self._register, job)

    def _register(self, connection: sa.Connection, job: QueryJob) -> QueryJob:
        operation = connection.execute(
            sa.select(tool_executions.c.operation_id)
            .where(tool_executions.c.operation_id == job.operation_id)
            .with_for_update()
        ).one_or_none()
        if operation is None:
            raise RecordNotFound("operation", job.operation_id)
        existing = self._submission(connection, job.operation_id, job.submission)
        if existing is not None:
            if existing != job:
                raise IdempotencyConflict("query job", job.operation_id)
            return existing
        latest = self._get(connection, job.operation_id)
        expected = 1 if latest is None else latest.submission + 1
        if job.submission != expected:
            raise IdempotencyConflict("query job", job.operation_id)
        try:
            with connection.begin_nested():
                row = connection.execute(
                    sa.insert(query_executions)
                    .values(
                        operation_id=job.operation_id,
                        submission=job.submission,
                        job_id=job.job_id,
                        project=job.project,
                        location=job.location,
                        query_fingerprint=job.query_fingerprint,
                        query_ref=job.query_ref,
                        authorization_version=job.authorization_version,
                        catalog_version=job.catalog_version,
                        registered_at=self._db.clock(),
                    )
                    .returning(*query_executions.c)
                ).one()
        except exc.IntegrityError:
            # The job ID already belongs to another operation or submission.
            raise IdempotencyConflict("query job", job.job_id) from None
        return _job(row)

    async def get_job(self, operation_id: str) -> QueryJob | None:
        return await self._db.transaction(self._get, operation_id)

    async def jobs(self, operation_id: str) -> Sequence[QueryJob]:
        return await self._db.transaction(self._jobs, operation_id)

    @staticmethod
    def _get(connection: sa.Connection, operation_id: str) -> QueryJob | None:
        row = connection.execute(
            sa.select(query_executions)
            .where(query_executions.c.operation_id == operation_id)
            .order_by(query_executions.c.submission.desc())
            .limit(1)
        ).one_or_none()
        return None if row is None else _job(row)

    @staticmethod
    def _submission(
        connection: sa.Connection, operation_id: str, submission: int
    ) -> QueryJob | None:
        row = connection.execute(
            sa.select(query_executions).where(
                query_executions.c.operation_id == operation_id,
                query_executions.c.submission == submission,
            )
        ).one_or_none()
        return None if row is None else _job(row)

    @staticmethod
    def _jobs(connection: sa.Connection, operation_id: str) -> list[QueryJob]:
        rows = connection.execute(
            sa.select(query_executions)
            .where(query_executions.c.operation_id == operation_id)
            .order_by(query_executions.c.submission)
        ).all()
        return [_job(row) for row in rows]
