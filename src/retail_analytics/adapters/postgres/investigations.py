"""PostgreSQL ``RunPrincipals`` and ``InvestigationInputs``.

Run-state changes that depend on pending input lock the run row first; adding
steering or an answer locks it too (shared), so a concurrent input is either
seen by the change or refused with ``RunNotActive``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.budgets import set_clarification_clock
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.runs import lock_run, transition_locked
from retail_analytics.adapters.postgres.schema import (
    run_inputs,
    run_principals,
    run_questions,
    runs,
)
from retail_analytics.adapters.postgres.sessions import insert_message
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.investigations import (
    AssistantOutput,
    RunClosure,
)
from retail_analytics.application.contracts.persistence import (
    IdempotencyConflict,
    RecordNotFound,
)
from retail_analytics.application.investigations import RunNotActive
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.investigations import (
    ClarificationQuestion,
    InputKind,
    InputStatus,
    QuestionStatus,
    RunInput,
    unapplied_message_id,
    unapplied_notice,
)
from retail_analytics.domain.runs import Run, RunStatus

_RUN_INPUT_KINDS = (InputKind.STEERING.value, InputKind.ANSWER.value)
_ACCEPTING = (RunStatus.RUNNING.value, RunStatus.WAITING_FOR_INPUT.value)


def _input(row: sa.Row[tuple[object, ...]]) -> RunInput:
    m = row._mapping
    return RunInput(
        input_id=m["input_id"],
        session_id=m["session_id"],
        kind=InputKind(m["kind"]),
        content=m["content"],
        status=InputStatus(m["status"]),
        created_at=m["created_at"],
        run_id=m["run_id"],
        message_id=m["message_id"],
        question_id=m["question_id"],
        applied_at=m["applied_at"],
        promoted_run_id=m["promoted_run_id"],
    )


def _question(row: sa.Row[tuple[object, ...]]) -> ClarificationQuestion:
    m = row._mapping
    return ClarificationQuestion(
        question_id=m["question_id"],
        run_id=m["run_id"],
        message_id=m["message_id"],
        status=QuestionStatus(m["status"]),
        asked_at=m["asked_at"],
        closed_at=m["closed_at"],
    )


class PostgresRunPrincipals:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, run_id: str, principal: Principal) -> Principal:
        return await self._db.transaction(self._record, run_id, principal)

    async def get(self, run_id: str) -> Principal | None:
        return await self._db.transaction(self._get, run_id)

    def _record(
        self, connection: sa.Connection, run_id: str, principal: Principal
    ) -> Principal:
        connection.execute(
            insert(run_principals)
            .values(
                run_id=run_id,
                executive_id=principal.executive_id,
                scopes=sorted(principal.scopes),
                recorded_at=self._db.clock(),
            )
            .on_conflict_do_nothing(index_elements=["run_id"])
        )
        stored = self._get(connection, run_id)
        if stored is None or stored.executive_id != principal.executive_id:
            raise IdempotencyConflict("run principal", run_id)
        return stored

    @staticmethod
    def _get(connection: sa.Connection, run_id: str) -> Principal | None:
        row = connection.execute(
            sa.select(run_principals).where(run_principals.c.run_id == run_id)
        ).one_or_none()
        if row is None:
            return None
        return Principal(row.executive_id, frozenset(row.scopes))


class PostgresInvestigationInputs:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, item: RunInput) -> RunInput:
        return await self._db.transaction(self._add, item)

    def _add(self, connection: sa.Connection, item: RunInput) -> RunInput:
        if item.kind.value in _RUN_INPUT_KINDS and item.run_id is not None:
            status = connection.execute(
                sa.select(runs.c.status)
                .where(runs.c.run_id == item.run_id)
                .with_for_update(read=True)
            ).scalar_one_or_none()
            existing = self._find(connection, item.input_id)
            if existing is None and status not in _ACCEPTING:
                raise RunNotActive(item.run_id)
        connection.execute(
            insert(run_inputs)
            .values(
                input_id=item.input_id,
                session_id=item.session_id,
                run_id=item.run_id,
                kind=item.kind.value,
                content=item.content,
                status=item.status.value,
                message_id=item.message_id,
                question_id=item.question_id,
                promoted_run_id=item.promoted_run_id,
                created_at=item.created_at,
                applied_at=item.applied_at,
            )
            .on_conflict_do_nothing(index_elements=["input_id"])
        )
        stored = self._find(connection, item.input_id)
        if stored is None:
            raise IdempotencyConflict("run input", item.input_id)
        if (
            stored.session_id,
            stored.run_id,
            stored.kind,
            stored.content,
            stored.question_id,
        ) != (
            item.session_id,
            item.run_id,
            item.kind,
            item.content,
            item.question_id,
        ):
            raise IdempotencyConflict("run input", item.input_id)
        return stored

    async def for_run(self, run_id: str) -> Sequence[RunInput]:
        return await self._db.transaction(self._for_run, run_id)

    async def pending(self, run_id: str) -> Sequence[RunInput]:
        return await self._db.transaction(self._pending, run_id)

    async def apply_pending(self, run_id: str) -> Sequence[RunInput]:
        return await self._db.transaction(self._apply_pending, run_id)

    def _apply_pending(
        self, connection: sa.Connection, run_id: str
    ) -> Sequence[RunInput]:
        connection.execute(
            sa.update(run_inputs)
            .where(
                run_inputs.c.run_id == run_id,
                run_inputs.c.status == InputStatus.PENDING.value,
                run_inputs.c.kind.in_(_RUN_INPUT_KINDS),
            )
            .values(status=InputStatus.APPLIED.value, applied_at=self._db.clock())
        )
        return self._for_run(connection, run_id)

    async def wait_for_input(
        self, question: ClarificationQuestion, *, content: str
    ) -> bool:
        return await self._db.transaction(self._wait, question, content)

    def _wait(
        self,
        connection: sa.Connection,
        question: ClarificationQuestion,
        content: str,
    ) -> bool:
        run = lock_run(connection, question.run_id)
        existing = connection.execute(
            sa.select(run_questions).where(
                run_questions.c.question_id == question.question_id
            )
        ).one_or_none()
        if existing is not None:
            return _question(existing).status is QuestionStatus.OPEN
        if self._pending(connection, question.run_id):
            return False
        if run.status is not RunStatus.RUNNING:
            raise RunNotActive(question.run_id)
        self._write_output(
            connection, run, AssistantOutput(question.message_id, content)
        )
        connection.execute(
            sa.insert(run_questions).values(
                question_id=question.question_id,
                run_id=question.run_id,
                message_id=question.message_id,
                status=QuestionStatus.OPEN.value,
                asked_at=question.asked_at,
            )
        )
        transition_locked(
            connection,
            question.run_id,
            RunStatus.WAITING_FOR_INPUT,
            at=self._db.clock(),
        )
        set_clarification_clock(
            connection, question.run_id, paused=True, at=self._db.clock()
        )
        return True

    async def resume_with_input(self, run_id: str) -> bool:
        return await self._db.transaction(self._resume, run_id)

    def _resume(self, connection: sa.Connection, run_id: str) -> bool:
        run = lock_run(connection, run_id)
        if run.status is RunStatus.RUNNING:
            return True
        if run.status is not RunStatus.WAITING_FOR_INPUT:
            return False
        if not self._pending(connection, run_id):
            return False
        self._close_question(connection, run_id, QuestionStatus.ANSWERED)
        transition_locked(connection, run_id, RunStatus.RUNNING, at=self._db.clock())
        set_clarification_clock(connection, run_id, paused=False, at=self._db.clock())
        return True

    async def close_run(
        self,
        run_id: str,
        to: RunStatus,
        *,
        output: AssistantOutput | None = None,
        force: bool = False,
    ) -> RunClosure:
        return await self._db.transaction(self._close_run, run_id, to, output, force)

    def _close_run(
        self,
        connection: sa.Connection,
        run_id: str,
        to: RunStatus,
        output: AssistantOutput | None,
        force: bool,
    ) -> RunClosure:
        if not to.is_terminal:
            raise ValueError("close_run needs a terminal status")
        run = lock_run(connection, run_id)
        if run.status.is_terminal:
            return RunClosure(run, closed=run.status is to)
        pending = self._pending(connection, run_id)
        if pending and not force:
            return RunClosure(run, closed=False)
        if pending:
            # Ending without another model step: the accepted input is not
            # applied. Recorded with the end and said in the closing message,
            # so it is never silently lost nor implied in the answer.
            notice = unapplied_notice(pending)
            output = (
                AssistantOutput(unapplied_message_id(run_id), notice)
                if output is None
                else AssistantOutput(output.message_id, f"{output.content}\n\n{notice}")
            )
            self._discard_pending(connection, run_id)
        if output is not None:
            self._write_output(connection, run, output)
        self._close_question(connection, run_id, QuestionStatus.CLOSED)
        updated = transition_locked(connection, run_id, to, at=self._db.clock())
        set_clarification_clock(connection, run_id, paused=True, at=self._db.clock())
        return RunClosure(updated, closed=True, unapplied=tuple(pending))

    def _write_output(
        self, connection: sa.Connection, run: Run, output: AssistantOutput
    ) -> None:
        insert_message(
            connection,
            message_id=output.message_id,
            session_id=run.session_id,
            role=MessageRole.ASSISTANT,
            content=output.content,
            run_id=run.run_id,
            at=self._db.clock(),
        )

    async def open_question(self, run_id: str) -> ClarificationQuestion | None:
        return await self._db.transaction(self._open_question, run_id)

    @staticmethod
    def _open_question(
        connection: sa.Connection, run_id: str
    ) -> ClarificationQuestion | None:
        row = connection.execute(
            sa.select(run_questions).where(
                run_questions.c.run_id == run_id,
                run_questions.c.status == QuestionStatus.OPEN.value,
            )
        ).one_or_none()
        return None if row is None else _question(row)

    def _close_question(
        self, connection: sa.Connection, run_id: str, to: QuestionStatus
    ) -> None:
        connection.execute(
            sa.update(run_questions)
            .where(
                run_questions.c.run_id == run_id,
                run_questions.c.status == QuestionStatus.OPEN.value,
            )
            .values(status=to.value, closed_at=self._db.clock())
        )

    async def discard_pending(self, run_id: str) -> int:
        return await self._db.transaction(self._discard_pending, run_id)

    @staticmethod
    def _discard_pending(connection: sa.Connection, run_id: str) -> int:
        result = connection.execute(
            sa.update(run_inputs)
            .where(
                run_inputs.c.run_id == run_id,
                run_inputs.c.status == InputStatus.PENDING.value,
                run_inputs.c.kind.in_(_RUN_INPUT_KINDS),
            )
            .values(status=InputStatus.DISCARDED.value)
        )
        return result.rowcount

    async def next_queued(self, session_id: str) -> RunInput | None:
        return await self._db.transaction(self._next_queued, session_id)

    @staticmethod
    def _next_queued(connection: sa.Connection, session_id: str) -> RunInput | None:
        row = connection.execute(
            sa.select(run_inputs)
            .where(
                run_inputs.c.session_id == session_id,
                run_inputs.c.kind == InputKind.QUEUED.value,
                run_inputs.c.status == InputStatus.PENDING.value,
            )
            .order_by(run_inputs.c.position)
            .limit(1)
        ).one_or_none()
        return None if row is None else _input(row)

    async def mark_promoted(self, input_id: str, run_id: str) -> RunInput:
        return await self._db.transaction(
            self._move, input_id, InputStatus.PROMOTED, run_id
        )

    async def discard(self, input_id: str) -> RunInput:
        return await self._db.transaction(
            self._move, input_id, InputStatus.DISCARDED, None
        )

    def _move(
        self,
        connection: sa.Connection,
        input_id: str,
        to: InputStatus,
        promoted_run_id: str | None,
    ) -> RunInput:
        row = connection.execute(
            sa.select(run_inputs)
            .where(run_inputs.c.input_id == input_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise RecordNotFound("run input", input_id)
        current = _input(row)
        updated = current.transition(
            to, at=self._db.clock(), promoted_run_id=promoted_run_id
        )
        if updated is None:
            if current.promoted_run_id != promoted_run_id:
                raise IdempotencyConflict("run input", input_id)
            return current
        connection.execute(
            sa.update(run_inputs)
            .where(run_inputs.c.input_id == input_id)
            .values(
                status=updated.status.value,
                applied_at=updated.applied_at,
                promoted_run_id=updated.promoted_run_id,
            )
        )
        return updated

    @staticmethod
    def _find(connection: sa.Connection, input_id: str) -> RunInput | None:
        row = connection.execute(
            sa.select(run_inputs).where(run_inputs.c.input_id == input_id)
        ).one_or_none()
        return None if row is None else _input(row)

    @staticmethod
    def _for_run(connection: sa.Connection, run_id: str) -> Sequence[RunInput]:
        rows = connection.execute(
            sa.select(run_inputs)
            .where(run_inputs.c.run_id == run_id)
            .order_by(run_inputs.c.position)
        ).all()
        return tuple(_input(r) for r in rows)

    @staticmethod
    def _pending(connection: sa.Connection, run_id: str) -> Sequence[RunInput]:
        rows = connection.execute(
            sa.select(run_inputs)
            .where(
                run_inputs.c.run_id == run_id,
                run_inputs.c.status == InputStatus.PENDING.value,
                run_inputs.c.kind.in_(_RUN_INPUT_KINDS),
            )
            .order_by(run_inputs.c.position)
        ).all()
        return tuple(_input(r) for r in rows)
