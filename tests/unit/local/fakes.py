"""Controlled collaborators for the local manager: no database, no Temporal.

Also imported by the import-guard subprocess (``test_without_temporal``), so
it must not import Temporal or pytest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from retail_analytics.adapters.local.investigations import LocalInvestigationManager
from retail_analytics.application.contracts.investigations import (
    AnswerDraft,
    BeginOutcome,
    CancelProgress,
    FinishRequest,
    InterruptionSweep,
    QuestionDraft,
    StepOutcome,
    StepResult,
    StopReason,
)
from retail_analytics.application.contracts.persistence import IdempotencyConflict
from retail_analytics.domain.request_scope import AdmissionDecision
from retail_analytics.domain.runs import ExecutionBackend, RunStatus, WorkflowRef
from tests.unit.agent.scenario import Scenario, scenario


@dataclass
class FakeRuntime:
    """``InvestigationRuntime`` steps over in-memory state, recording calls."""

    provider_switch: Scenario | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    status: dict[str, RunStatus] = field(default_factory=dict)
    pending: dict[str, int] = field(default_factory=dict)
    answers: list[AnswerDraft] = field(default_factory=list)
    questions: list[QuestionDraft] = field(default_factory=list)
    fail_release: bool = False

    def _call(self, step: str, run_id: str) -> None:
        self.calls.append((step, run_id))

    def steps(self, run_id: str) -> list[str]:
        return [step for step, run in self.calls if run == run_id]

    def add_input(self, run_id: str) -> None:
        self.pending[run_id] = self.pending.get(run_id, 0) + 1

    async def begin(self, run_id: str) -> BeginOutcome:
        self._call("begin", run_id)
        self.status.setdefault(run_id, RunStatus.RUNNING)
        return BeginOutcome(self.status[run_id], AdmissionDecision.PROCEED)

    async def release_answer(self, draft: AnswerDraft) -> StepOutcome:
        self._call("release_answer", draft.run_id)
        if self.fail_release:
            raise RuntimeError("store unavailable")
        if self.status[draft.run_id] is RunStatus.CANCELLING:
            return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.CANCELLED)
        self.answers.append(draft)
        self.status[draft.run_id] = RunStatus.COMPLETED
        return StepOutcome(StepResult.RELEASED, status=RunStatus.COMPLETED)

    async def ask(self, draft: QuestionDraft) -> StepOutcome:
        self._call("ask", draft.run_id)
        self.questions.append(draft)
        self.status[draft.run_id] = RunStatus.WAITING_FOR_INPUT
        return StepOutcome(StepResult.ASKED, question_id=f"q{draft.sequence}")

    async def resume(self, run_id: str) -> StepOutcome:
        self._call("resume", run_id)
        if self.status[run_id] is RunStatus.CANCELLING:
            return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.CANCELLED)
        if not self.pending.pop(run_id, 0):
            return StepOutcome(StepResult.IDLE)
        self.status[run_id] = RunStatus.RUNNING
        if self.provider_switch is not None:
            self.provider_switch.provider.ends_with = "answer"
        return StepOutcome(StepResult.CONTINUE)

    async def expire(self, run_id: str) -> StepOutcome:
        self._call("expire", run_id)
        self.status[run_id] = RunStatus.FAILED
        return StepOutcome(StepResult.STOPPED)

    async def finish_partial(self, request: FinishRequest) -> StepOutcome:
        self._call("finish_partial:" + request.reason.value, request.run_id)
        self.status[request.run_id] = RunStatus.FAILED
        return StepOutcome(StepResult.RELEASED, status=RunStatus.FAILED)

    async def finish_message(self, run_id: str, message: str) -> StepOutcome:
        self._call("finish_message", run_id)
        self.status[run_id] = RunStatus.COMPLETED
        return StepOutcome(StepResult.RELEASED)

    async def begin_cancel(self, run_id: str) -> CancelProgress:
        self._call("begin_cancel", run_id)
        self.status[run_id] = RunStatus.CANCELLING
        return CancelProgress(0)

    async def reconcile_cancel(self, run_id: str) -> CancelProgress:
        self._call("reconcile_cancel", run_id)
        return CancelProgress(0)

    async def finish_cancelled(self, run_id: str, *, settled: bool) -> StepOutcome:
        self._call("finish_cancelled", run_id)
        self.status[run_id] = RunStatus.CANCELLED
        return StepOutcome(StepResult.STOPPED, status=RunStatus.CANCELLED)

    async def interrupt(self, run_id: str) -> StepOutcome:
        self._call("interrupt", run_id)
        if self.status.get(run_id, RunStatus.RUNNING).is_active:
            self.status[run_id] = RunStatus.FAILED
        return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.INTERRUPTED)

    def cancel(self, run_id: str) -> None:
        """What ``InvestigationControl.cancel`` persists before notifying."""
        self.status[run_id] = RunStatus.CANCELLING


@dataclass
class FakeRuns:
    """``RunRepository.attach_workflow`` with Temporal-owned runs."""

    temporal: set[str] = field(default_factory=set)
    attached: dict[str, WorkflowRef] = field(default_factory=dict)

    async def attach_workflow(self, run_id: str, workflow: WorkflowRef) -> Any:
        if run_id in self.temporal or workflow.backend is not ExecutionBackend.LOCAL:
            raise IdempotencyConflict("run execution backend", run_id)
        current = self.attached.setdefault(run_id, workflow)
        if current != workflow:
            raise IdempotencyConflict("run workflow", run_id)
        return None


class LockHeld(Exception):
    pass


@dataclass
class FakeLock:
    held_elsewhere: bool = False
    held: bool = False

    async def acquire(self) -> None:
        if self.held_elsewhere:
            raise LockHeld
        self.held = True

    async def release(self) -> None:
        self.held = False


@dataclass
class FakeSweep:
    owners: list[str] = field(default_factory=list)

    async def sweep(self, *, owner: str) -> InterruptionSweep:
        self.owners.append(owner)
        return InterruptionSweep()


@dataclass
class Harness:
    manager: LocalInvestigationManager
    runtime: FakeRuntime
    runs: FakeRuns
    lock: FakeLock
    sweep: FakeSweep
    agent: Scenario


def harness(*, ends_with: str = "answer", **options: Any) -> Harness:
    agent = scenario(ends_with=ends_with)
    runtime = FakeRuntime(provider_switch=agent)
    runs, lock, sweep = FakeRuns(), FakeLock(), FakeSweep()
    manager = LocalInvestigationManager(runs=cast(Any, runs), lock=lock, **options)
    manager.bind(runtime=cast(Any, runtime), agent=agent.agent, sweep=cast(Any, sweep))
    return Harness(manager, runtime, runs, lock, sweep, agent)
