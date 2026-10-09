"""In-process investigation manager: the local execution backend.

``LocalInvestigationManager`` implements the ``InvestigationScheduler`` port
for runs created for ``ExecutionBackend.LOCAL``. Each started run is one
asyncio task owned by the manager - and so by the application lifespan that
opens and closes it - never by an HTTP request or an SSE connection: clients
disconnect and reconnect (replaying persisted events) without affecting it.

The manager executes; it does not decide. Like the Temporal workflow, it runs
the ``InvestigationRuntime`` steps and the shared agent
(``adapters.agent.investigator``) and asks ``application.investigation_lifecycle``
what follows each step. Authorization, budgets, context, output gates,
clarification, steering, queueing and cancellation stay in those application
modules. What differs is durability:

- Waiting for clarification is an in-memory wait on notifications (no model
  call, no polling); a notification only says "look at persisted input".
- Process lifetime only. No step is retried by the manager and nothing is
  replayed: when the process stops, ``close`` stops admission, cancels the
  tasks and ends their runs through ``InvestigationRuntime.interrupt`` within
  a bounded grace period (running warehouse jobs get cancellation requested
  by their recorded job reference; unconfirmed ones are reported as such).
  When the process dies instead, the next ``open`` sweeps the orphaned local
  runs the same way before admitting work (``InterruptedRunSweep``).
- One manager per database: ``open`` takes a process-lifetime lock and fails
  clearly when another manager holds it. It is not a distributed scheduler.
- Ownership: every run it starts is recorded as owned by this manager
  instance; a run of another backend (Temporal) is refused, never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Protocol, Self

from pydantic_ai.run import AgentRunResult

from retail_analytics.adapters.agent.investigator import (
    InvestigationAgent,
    InvestigationOutput,
    proposal,
    run_investigation,
)
from retail_analytics.application import investigation_lifecycle as lifecycle
from retail_analytics.application.contracts.investigations import (
    AgentInterruption,
    AnswerDraft,
    FinishRequest,
    InterruptionKind,
    InterruptionSweep,
    LifecycleAction,
    LifecycleDecision,
    QuestionDraft,
    StopReason,
)
from retail_analytics.application.investigation_interruption import (
    InterruptedRunSweep,
)
from retail_analytics.application.investigation_runtime import (
    InvestigationContextChanged,
    InvestigationRuntime,
    RunStopped,
)
from retail_analytics.application.ports.persistence import RunRepository
from retail_analytics.domain.runs import ExecutionBackend, WorkflowRef

_log = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 4
DEFAULT_SHUTDOWN_GRACE = timedelta(seconds=10)


class ManagerLock(Protocol):
    """Process-lifetime exclusion of other local managers (one per store)."""

    async def acquire(self) -> None: ...

    async def release(self) -> None: ...


class ManagerNotBound(Exception):
    """The manager has no runtime/agent/sweep: it must not execute work."""


@dataclass(frozen=True)
class _Bound:
    runtime: InvestigationRuntime
    agent: InvestigationAgent
    sweep: InterruptedRunSweep


class _Signals:
    """Notifications for one run: input recorded, cancellation requested."""

    def __init__(self) -> None:
        self.generation = 0
        self.cancelled = False
        self._changed = asyncio.Event()

    def input_available(self) -> None:
        self.generation += 1
        self._changed.set()

    def cancel(self) -> None:
        self.cancelled = True
        self._changed.set()

    async def wait_for_input(self, generation: int) -> None:
        """Until input is notified after ``generation`` (or cancellation)."""
        while not (self.cancelled or self.generation != generation):
            self._changed.clear()
            await self._changed.wait()

    async def wait_cancelled(self) -> None:
        while not self.cancelled:
            self._changed.clear()
            await self._changed.wait()


def _causes(error: BaseException) -> Iterator[BaseException]:
    seen: list[BaseException] = []
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if any(current is s for s in seen):
            continue
        seen.append(current)
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)


def interruption(error: BaseException) -> AgentInterruption:
    """The runtime-neutral reason an agent run failed with ``error``."""
    for current in _causes(error):
        if isinstance(current, InvestigationContextChanged):
            return AgentInterruption(InterruptionKind.CONTEXT_CHANGED)
        if isinstance(current, RunStopped):
            return AgentInterruption(
                InterruptionKind.STOPPED, current.reason, current.resource
            )
    return AgentInterruption(InterruptionKind.FAILED)


class LocalInvestigationManager:
    """Runs local investigations in this process (``InvestigationScheduler``).

    Construct, ``bind`` (after the services exist: they need this scheduler),
    then use as ``async with manager:`` (``open``/``close``) for the
    application lifespan.
    """

    def __init__(
        self,
        *,
        runs: RunRepository,
        lock: ManagerLock,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        shutdown_grace: timedelta = DEFAULT_SHUTDOWN_GRACE,
        wait_limit: timedelta = lifecycle.WAIT_LIMIT,
        cancel_settle_interval: timedelta = lifecycle.CANCEL_SETTLE_INTERVAL,
        instance_id: str | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._runs = runs
        self._lock = lock
        self._slots = asyncio.Semaphore(max_concurrent)
        self._shutdown_grace = shutdown_grace
        self._wait_limit = wait_limit
        self._settle_interval = cancel_settle_interval
        self._instance_id = instance_id or "local-" + uuid.uuid4().hex
        self._bound: _Bound | None = None
        self._admitting = False
        self._executions: dict[str, tuple[_Signals, asyncio.Task[None]]] = {}
        # Runs this instance has executed: a later start never runs them again.
        self._started: set[str] = set()

    # Construction and lifespan

    @property
    def backend(self) -> ExecutionBackend:
        return ExecutionBackend.LOCAL

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def admitting(self) -> bool:
        return self._admitting

    def running(self) -> frozenset[str]:
        """Runs with a live task in this process."""
        return frozenset(self._executions)

    def bind(
        self,
        *,
        runtime: InvestigationRuntime,
        agent: InvestigationAgent,
        sweep: InterruptedRunSweep,
    ) -> None:
        self._bound = _Bound(runtime, agent, sweep)

    def _require(self) -> _Bound:
        if self._bound is None:
            raise ManagerNotBound("local investigation manager is not bound")
        return self._bound

    async def open(self) -> InterruptionSweep:
        """Take the manager lock, end orphaned local runs, then admit work.

        Raises ``ManagerNotBound`` or the lock's error (another manager is
        running) without admitting anything.
        """
        bound = self._require()
        await self._lock.acquire()
        try:
            swept = await bound.sweep.sweep(owner=self._instance_id)
        except BaseException:
            await self._lock.release()
            raise
        self._admitting = True
        return swept

    async def close(self) -> None:
        """Stop admission and end owned work truthfully within the grace period.

        Running runs end interrupted (``InvestigationRuntime.interrupt``). A
        run that cannot be ended in time stays active in the store and is
        swept by the next ``open``; nothing waits indefinitely.
        """
        self._admitting = False
        owned = dict(self._executions)
        for _, task in owned.values():
            task.cancel()
        try:
            async with asyncio.timeout(self._shutdown_grace.total_seconds()):
                await asyncio.gather(
                    *(task for _, task in owned.values()), return_exceptions=True
                )
                if self._bound is not None:
                    ended = await asyncio.gather(
                        *(self._bound.runtime.interrupt(r) for r in owned),
                        return_exceptions=True,
                    )
                    if any(isinstance(e, BaseException) for e in ended):
                        _log.warning(
                            "some local investigations could not be ended; "
                            "swept at startup"
                        )
        except TimeoutError:
            _log.warning("local investigations not ended in time; swept at startup")
        finally:
            await self._lock.release()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    # InvestigationScheduler

    async def start(self, run_id: str) -> WorkflowRef:
        reference = WorkflowRef(self._instance_id, None, ExecutionBackend.LOCAL)
        # Refuses (IdempotencyConflict) a run of another backend or manager.
        await self._runs.attach_workflow(run_id, reference)
        if run_id in self._executions or run_id in self._started:
            return reference
        if not self._admitting:
            # Shutting down (or never opened): end it now rather than leave
            # it waiting for an execution that will not come.
            await self._require().runtime.interrupt(run_id)
            return reference
        bound = self._require()
        signals = _Signals()
        task = asyncio.create_task(
            self._execute(bound, run_id, signals), name="investigation"
        )
        self._executions[run_id] = (signals, task)
        self._started.add(run_id)
        task.add_done_callback(lambda _: self._executions.pop(run_id, None))
        return reference

    async def notify_input(self, run_id: str) -> None:
        execution = self._executions.get(run_id)
        if execution is not None:
            execution[0].input_available()

    async def request_cancel(self, run_id: str) -> None:
        execution = self._executions.get(run_id)
        if execution is not None:
            execution[0].cancel()

    # Execution: lifecycle decisions carried out with asyncio

    async def _execute(self, bound: _Bound, run_id: str, signals: _Signals) -> None:
        try:
            await self._drive(bound, run_id, signals)
        except asyncio.CancelledError:
            raise  # shutdown: ``close`` records the outcome
        except Exception:
            # A step failed (store unavailable, a defect): no automatic retry
            # or replay. End the run with its verified findings if possible.
            _log.warning("local investigation step failed; run stopped")
            try:
                await bound.runtime.finish_partial(
                    FinishRequest(run_id, StopReason.INTERRUPTED)
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await bound.runtime.interrupt(run_id)

    async def _drive(self, bound: _Bound, run_id: str, signals: _Signals) -> None:
        runtime = bound.runtime
        async with self._slots:
            admission = lifecycle.admit(await runtime.begin(run_id))
        if admission.action is LifecycleAction.CLOSE:
            return
        if admission.cancelling:
            signals.cancel()
        if admission.action is LifecycleAction.FINISH_MESSAGE:
            await runtime.finish_message(run_id, admission.message or "")
            return
        waiting = admission.action is LifecycleAction.AWAIT_INPUT
        sequence = 0
        if admission.action is LifecycleAction.ASK:
            asked = await runtime.ask(
                QuestionDraft(run_id, sequence, admission.message or "")
            )
            waiting = (
                lifecycle.after_admission_question(asked).action
                is LifecycleAction.AWAIT_INPUT
            )
            sequence += 1
        while not signals.cancelled:
            if waiting:
                # Check persisted input before waiting: a notification that
                # arrived during the previous step is not lost.
                generation = signals.generation
                resumed = lifecycle.after_resume(await runtime.resume(run_id))
                if resumed.action is LifecycleAction.CANCEL:
                    signals.cancel()
                    break
                if resumed.action is LifecycleAction.WAIT:
                    try:
                        async with asyncio.timeout(self._wait_limit.total_seconds()):
                            await signals.wait_for_input(generation)
                        notified = True
                    except TimeoutError:
                        notified = False
                    if not notified:
                        expiry = lifecycle.after_wait_expired(
                            await runtime.expire(run_id)
                        )
                        if expiry.action is LifecycleAction.AWAIT_INPUT:
                            continue
                        return
                    continue
                waiting = False
            result = await self._investigate(bound, run_id, signals)
            if result is None:
                break  # cancelled while the agent was working
            if isinstance(result, AgentInterruption):
                failed = lifecycle.after_interruption(result)
                if failed.action is LifecycleAction.INVESTIGATE:
                    continue
                if failed.action is LifecycleAction.CANCEL:
                    signals.cancel()
                    break
                await self._stop(runtime, run_id, failed)
                return
            draft = proposal(run_id, sequence, result)
            if isinstance(draft, AnswerDraft):
                outcome = await runtime.release_answer(draft)
            else:
                outcome = await runtime.ask(draft)
            sequence += 1
            released = lifecycle.after_output(outcome)
            if released.action is LifecycleAction.CLOSE:
                return
            if released.action is LifecycleAction.CANCEL:
                signals.cancel()
                break
            if released.action is LifecycleAction.STOP:
                await self._stop(runtime, run_id, released)
                return
            waiting = released.action is LifecycleAction.AWAIT_INPUT
            # A fresh agent run receives rebuilt context. Old model messages
            # cannot carry revoked evidence or superseded assumptions forward.
        await self._cancel(runtime, run_id)

    async def _investigate(
        self, bound: _Bound, run_id: str, signals: _Signals
    ) -> AgentRunResult[InvestigationOutput] | AgentInterruption | None:
        """One agent run; None when cancellation was requested meanwhile."""
        async with self._slots:
            if signals.cancelled:
                return None
            work = asyncio.create_task(run_investigation(bound.agent, run_id))
            cancelled = asyncio.create_task(signals.wait_cancelled())
            try:
                await asyncio.wait(
                    {work, cancelled}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (work, cancelled):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task
            if work.cancelled():
                return None
            error = work.exception()
            if error is None:
                return work.result()
            if not isinstance(error, Exception):
                raise error
            return interruption(error)

    async def _stop(
        self, runtime: InvestigationRuntime, run_id: str, decision: LifecycleDecision
    ) -> None:
        await runtime.finish_partial(
            FinishRequest(
                run_id,
                decision.stop_reason or StopReason.INTERRUPTED,
                decision.resource,
            )
        )

    async def _cancel(self, runtime: InvestigationRuntime, run_id: str) -> None:
        progress = await runtime.begin_cancel(run_id)
        for _ in range(lifecycle.CANCEL_SETTLE_CHECKS):
            if lifecycle.cancellation_settled(progress):
                break
            await asyncio.sleep(self._settle_interval.total_seconds())
            progress = await runtime.reconcile_cancel(run_id)
        await runtime.finish_cancelled(
            run_id, settled=lifecycle.cancellation_settled(progress)
        )
