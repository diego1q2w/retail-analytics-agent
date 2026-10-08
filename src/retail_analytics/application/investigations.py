"""Starting, steering, answering, queueing, cancelling and attaching to
investigations: the use cases an API (or any client boundary) calls.

An investigation is one run executed durably by the runtime (a workflow per
run). This module never executes analysis itself; it records what the user
asked for and tells the runtime through the narrow ``InvestigationScheduler``
port. Every input is persisted *before* the runtime is notified and the
notification carries no content, so a lost or repeated signal cannot lose or
duplicate input, and the workflow history never holds user text it does not
need.

Rules enforced here, on every call:

- the caller must own the session/run (not-found and not-owned look alike)
  and, for anything that starts or changes analysis, hold ``analysis:read``;
- client submission keys make every call idempotent (same key, same record);
- one active run per session: a new message steers the active run by default,
  an explicitly queued request waits for it (``submit``/``enqueue``);
- input to a run that is no longer active is refused atomically
  (``RunNotActive``), never silently attached to a finished run;
- disconnecting does nothing: runs continue in the runtime, and ``attach``
  returns the persisted progress after a cursor plus any open question.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
    OwnershipGuard,
    Principal,
)
from retail_analytics.application.persistence import (
    ActiveRunExists,
    RunEventStore,
    RunRepository,
    RunRequest,
    SessionRepository,
)
from retail_analytics.application.progress import ProgressEvent
from retail_analytics.domain.access import Permission
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.investigations import (
    MAX_INPUT_CHARS,
    ClarificationQuestion,
    InputKind,
    InputStatus,
    QuestionStatus,
    RunInput,
    input_id_for,
    message_id_for,
    run_id_for,
)
from retail_analytics.domain.runs import Run, RunStatus, WorkflowRef

# Ports


class RunPrincipals(Protocol):
    """Who a run acts for: the authenticated executive and token scope ceiling.

    Retried activities re-resolve current authority from this (never from a
    token or a workflow payload). The first record wins; recording again for
    the same run is a no-op.
    """

    async def record(self, run_id: str, principal: Principal) -> Principal: ...

    async def get(self, run_id: str) -> Principal | None: ...


class RunNotActive(Exception):
    """The run has ended (or is being cancelled); it accepts no more input."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id!r} is not active")


@dataclass(frozen=True, slots=True)
class AssistantOutput:
    """A released assistant message written together with a run-state change."""

    message_id: str
    content: str

    def __repr__(self) -> str:
        return f"AssistantOutput({self.message_id!r}, <{len(self.content)} chars>)"


@dataclass(frozen=True, slots=True)
class RunClosure:
    run: Run
    # False when pending input kept the run open (nothing changed).
    closed: bool


class InvestigationInputs(Protocol):
    """The input log of runs and the run-state changes that must see it.

    Methods that change run state lock the run and check pending input in the
    same transaction, so input recorded concurrently is either seen by the
    change or refused with ``RunNotActive``; it is never silently dropped.
    """

    async def add(self, item: RunInput) -> RunInput:
        """Record once per input ID (a repeat returns the original; different
        content raises ``IdempotencyConflict``). Steering and answers raise
        ``RunNotActive`` unless the run is running or waiting for input."""
        ...

    async def for_run(self, run_id: str) -> Sequence[RunInput]:
        """Every input of the run, in arrival order."""
        ...

    async def pending(self, run_id: str) -> Sequence[RunInput]: ...

    async def apply_pending(self, run_id: str) -> Sequence[RunInput]:
        """Mark pending steering/answers applied; return all inputs in order."""
        ...

    async def wait_for_input(
        self, question: ClarificationQuestion, *, content: str
    ) -> bool:
        """Open the question, write it as the run's assistant message and move
        the run to waiting - unless input is already pending (then nothing
        changes and False is returned). Repeating an open question is True."""
        ...

    async def resume_with_input(self, run_id: str) -> bool:
        """If input is pending, close the open question (answered) and move a
        waiting run back to running; False (no change) otherwise."""
        ...

    async def close_run(
        self,
        run_id: str,
        to: RunStatus,
        *,
        output: AssistantOutput | None = None,
        force: bool = False,
    ) -> RunClosure:
        """Move the run to the terminal status ``to`` and write ``output`` as
        its assistant message, in one transaction - unless steering/answer
        input is pending and ``force`` is False (then nothing changes). An
        open question is closed. A run already in ``to`` is reported closed."""
        ...

    async def open_question(self, run_id: str) -> ClarificationQuestion | None: ...

    async def discard_pending(self, run_id: str) -> int:
        """Discard input that can no longer be applied (the run ended)."""
        ...

    async def next_queued(self, session_id: str) -> RunInput | None:
        """The oldest pending queued request of the session."""
        ...

    async def mark_promoted(self, input_id: str, run_id: str) -> RunInput: ...

    async def discard(self, input_id: str) -> RunInput: ...


class InvestigationScheduler(Protocol):
    """Port to the durable runtime (a Temporal workflow per run).

    Notifications carry identifiers only; the runtime reads persisted input.
    ``start`` is idempotent per run.
    """

    async def start(self, run_id: str) -> WorkflowRef: ...

    async def notify_input(self, run_id: str) -> None: ...

    async def request_cancel(self, run_id: str) -> None: ...


# Results


class SubmitMode(StrEnum):
    # Default: refine the session's active run, or start one if none is active.
    STEER = "steer"
    # Explicitly a separate request: start now or wait behind the active run.
    QUEUE = "queue"


@dataclass(frozen=True, slots=True)
class RunHandle:
    run_id: str
    status: RunStatus
    # False when the submission key matched an earlier request.
    created: bool


@dataclass(frozen=True, slots=True)
class InputReceipt:
    input_id: str
    kind: InputKind
    # The run the input belongs to (for a queued request: the run it became,
    # if it could start at once; otherwise None while it waits).
    run_id: str | None


@dataclass(frozen=True, slots=True)
class Attachment:
    """What a (re)connecting client needs: status, events after its cursor
    and the question the run waits on, if any."""

    run: Run
    events: tuple[ProgressEvent, ...]
    open_question_id: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clean(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("message must not be empty")
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError("message is too long")
    return text


# Starting runs (shared by the API path and queued-request promotion)


class InvestigationLauncher:
    """Creates a run with its principal and request, then schedules it."""

    def __init__(
        self,
        *,
        runs: RunRepository,
        principals: RunPrincipals,
        inputs: InvestigationInputs,
        scheduler: InvestigationScheduler,
        resolver: AccessResolver,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._runs = runs
        self._principals = principals
        self._inputs = inputs
        self._scheduler = scheduler
        self._resolver = resolver
        self._clock = clock

    async def launch(
        self,
        principal: Principal,
        *,
        session_id: str,
        text: str,
        submission_key: str,
    ) -> RunHandle:
        """Start (or find) the run of this submission and schedule it.

        Raises ``ActiveRunExists`` when another run of the session is active.
        Ownership and permission are the caller's job.
        """
        run_id = run_id_for(session_id, submission_key)
        request_input = input_id_for(run_id, "request")
        await self._principals.record(run_id, principal)
        started = await self._runs.start_run(
            RunRequest(
                run_id=run_id,
                session_id=session_id,
                requested_by=principal.executive_id,
                submission_key=submission_key,
                message_id=message_id_for(request_input),
                request_text=text,
            )
        )
        run = started.run
        await self._inputs.add(
            RunInput(
                input_id=request_input,
                session_id=session_id,
                kind=InputKind.REQUEST,
                content=text,
                status=InputStatus.APPLIED,
                created_at=self._clock(),
                run_id=run.run_id,
                message_id=run.trigger_message_id,
                applied_at=self._clock(),
            )
        )
        if run.status.is_active:
            workflow = await self._scheduler.start(run.run_id)
            run = await self._runs.attach_workflow(run.run_id, workflow)
        return RunHandle(run.run_id, run.status, started.created)

    async def promote_next(self, session_id: str) -> str | None:
        """Start the session's oldest queued request if no run is active.

        Safe to call from several places at once: the one-active-run rule and
        idempotent run creation make concurrent promotions converge on one
        run. A queued request whose executive lost analysis access is
        discarded instead of started.
        """
        queued = await self._inputs.next_queued(session_id)
        if queued is None:
            return None
        active = await self._runs.active_run(session_id)
        key = "queued:" + queued.input_id
        if active is not None:
            if active.run_id == run_id_for(session_id, key):
                await self._inputs.mark_promoted(queued.input_id, active.run_id)
                return active.run_id
            return None
        principal = await self._principals.get(run_id_for(session_id, key))
        if principal is None:
            await self._inputs.discard(queued.input_id)
            return None
        try:
            await self._resolver.require_permission(principal, Permission.ANALYSIS_READ)
        except AccessDenied:
            await self._inputs.discard(queued.input_id)
            return None
        try:
            handle = await self.launch(
                principal,
                session_id=session_id,
                text=queued.content,
                submission_key=key,
            )
        except ActiveRunExists:
            return None
        await self._inputs.mark_promoted(queued.input_id, handle.run_id)
        if handle.status.is_terminal:
            return await self.promote_next(session_id)
        return handle.run_id


# Client-facing use cases


class InvestigationControl:
    def __init__(
        self,
        *,
        resolver: AccessResolver,
        guard: OwnershipGuard,
        sessions: SessionRepository,
        runs: RunRepository,
        principals: RunPrincipals,
        inputs: InvestigationInputs,
        events: RunEventStore,
        scheduler: InvestigationScheduler,
        launcher: InvestigationLauncher,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._resolver = resolver
        self._guard = guard
        self._sessions = sessions
        self._runs = runs
        self._principals = principals
        self._inputs = inputs
        self._events = events
        self._scheduler = scheduler
        self._launcher = launcher
        self._clock = clock

    async def start(
        self,
        principal: Principal,
        *,
        session_id: str,
        text: str,
        submission_key: str,
    ) -> RunHandle:
        """Start an investigation in the caller's session.

        Raises ``AccessDenied`` (session not the caller's, or no analysis
        permission) and ``ActiveRunExists`` (use ``submit`` to steer or queue).
        """
        await self._resolver.require_permission(principal, Permission.ANALYSIS_READ)
        await self._guard.session(principal.executive_id, session_id)
        return await self._launcher.launch(
            principal,
            session_id=session_id,
            text=_clean(text),
            submission_key=submission_key,
        )

    async def submit(
        self,
        principal: Principal,
        *,
        session_id: str,
        text: str,
        submission_key: str,
        mode: SubmitMode = SubmitMode.STEER,
    ) -> InputReceipt:
        """A new message in a session: steers the active run by default, or is
        queued as a separate request when ``mode`` is QUEUE. Starts a run when
        none is active."""
        if mode is SubmitMode.QUEUE:
            return await self.enqueue(
                principal,
                session_id=session_id,
                text=text,
                submission_key=submission_key,
            )
        await self._resolver.require_permission(principal, Permission.ANALYSIS_READ)
        await self._guard.session(principal.executive_id, session_id)
        active = await self._runs.active_run(session_id)
        if active is not None and active.status is not RunStatus.CANCELLING:
            try:
                return await self.steer(
                    principal,
                    run_id=active.run_id,
                    text=text,
                    submission_key=submission_key,
                )
            except RunNotActive:
                pass
        handle = await self.start(
            principal, session_id=session_id, text=text, submission_key=submission_key
        )
        return InputReceipt(
            input_id_for(handle.run_id, "request"), InputKind.REQUEST, handle.run_id
        )

    async def steer(
        self,
        principal: Principal,
        *,
        run_id: str,
        text: str,
        submission_key: str,
    ) -> InputReceipt:
        """Refine the active run; applied at its next safe boundary."""
        return await self._add_run_input(
            principal, run_id, InputKind.STEERING, text, submission_key
        )

    async def answer(
        self,
        principal: Principal,
        *,
        run_id: str,
        question_id: str,
        text: str,
        submission_key: str,
    ) -> InputReceipt:
        """Answer the run's open clarification question.

        Raises ``RunNotActive`` when the question is no longer open.
        """
        await self._authorize_run(principal, run_id, analysis=True)
        question = await self._inputs.open_question(run_id)
        if question is None or question.question_id != question_id:
            raise RunNotActive(run_id)
        return await self._add_run_input(
            principal,
            run_id,
            InputKind.ANSWER,
            text,
            submission_key,
            question_id=question_id,
        )

    async def enqueue(
        self,
        principal: Principal,
        *,
        session_id: str,
        text: str,
        submission_key: str,
    ) -> InputReceipt:
        """Queue a separate request behind the session's active run (or start
        it now if none is active). Queued requests never steer."""
        await self._resolver.require_permission(principal, Permission.ANALYSIS_READ)
        await self._guard.session(principal.executive_id, session_id)
        input_id = input_id_for(session_id, submission_key)
        # The principal is recorded for the run the request will become.
        await self._principals.record(
            run_id_for(session_id, "queued:" + input_id), principal
        )
        await self._inputs.add(
            RunInput(
                input_id=input_id,
                session_id=session_id,
                kind=InputKind.QUEUED,
                content=_clean(text),
                status=InputStatus.PENDING,
                created_at=self._clock(),
            )
        )
        own_run = run_id_for(session_id, "queued:" + input_id)
        started = await self._launcher.promote_next(session_id)
        return InputReceipt(
            input_id, InputKind.QUEUED, started if started == own_run else None
        )

    async def cancel(self, principal: Principal, *, run_id: str) -> Run:
        """Stop scheduling new work; in-flight effects are reconciled by the
        runtime, which reports the run cancelled once they are settled."""
        await self._authorize_run(principal, run_id, analysis=False)
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AccessDenied("run", run_id)
        if run.status.is_terminal:
            return run
        run = await self._runs.transition_run(run_id, RunStatus.CANCELLING)
        await self._scheduler.request_cancel(run_id)
        return run

    async def attach(
        self,
        principal: Principal,
        *,
        run_id: str,
        after_event_id: str | None = None,
        limit: int = 500,
    ) -> Attachment:
        """Re-attach after a disconnect: never starts or restarts anything."""
        await self._authorize_run(principal, run_id, analysis=False)
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AccessDenied("run", run_id)
        events = await self._events.replay(
            run_id, after_event_id=after_event_id, limit=limit
        )
        question = await self._inputs.open_question(run_id)
        return Attachment(
            run, tuple(events), None if question is None else question.question_id
        )

    async def _authorize_run(
        self, principal: Principal, run_id: str, *, analysis: bool
    ) -> None:
        context = await self._resolver.context_for_run(principal, run_id)
        if analysis and Permission.ANALYSIS_READ.value not in context.permissions:
            raise AccessDenied("permission", Permission.ANALYSIS_READ.value)

    async def _add_run_input(
        self,
        principal: Principal,
        run_id: str,
        kind: InputKind,
        text: str,
        submission_key: str,
        *,
        question_id: str | None = None,
    ) -> InputReceipt:
        await self._authorize_run(principal, run_id, analysis=True)
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AccessDenied("run", run_id)
        if run.status not in (RunStatus.RUNNING, RunStatus.WAITING_FOR_INPUT):
            raise RunNotActive(run_id)
        content = _clean(text)
        input_id = input_id_for(run_id, submission_key)
        message_id = message_id_for(input_id)
        # Recorded first: refused atomically if the run ended meanwhile, so a
        # message is only added to the conversation for input the run accepted.
        await self._inputs.add(
            RunInput(
                input_id=input_id,
                session_id=run.session_id,
                kind=kind,
                content=content,
                status=InputStatus.PENDING,
                created_at=self._clock(),
                run_id=run_id,
                message_id=message_id,
                question_id=question_id,
            )
        )
        await self._sessions.append_message(
            message_id=message_id,
            session_id=run.session_id,
            role=MessageRole.USER,
            content=content,
            run_id=run_id,
        )
        await self._scheduler.notify_input(run_id)
        return InputReceipt(input_id, kind, run_id)


__all__ = [
    "ActiveRunExists",
    "AssistantOutput",
    "Attachment",
    "ClarificationQuestion",
    "InputReceipt",
    "InvestigationControl",
    "InvestigationInputs",
    "InvestigationLauncher",
    "InvestigationScheduler",
    "QuestionStatus",
    "RunClosure",
    "RunHandle",
    "RunNotActive",
    "RunPrincipals",
    "SubmitMode",
]
