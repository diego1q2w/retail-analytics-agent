from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.investigations import (
    AssistantOutput,
    RunClosure,
)
from retail_analytics.domain.investigations import (
    ClarificationQuestion,
    RunInput,
)
from retail_analytics.domain.runs import (
    RunStatus,
    WorkflowRef,
)


# Ports
class RunPrincipals(Protocol):
    """Who a run acts for: the authenticated executive and token scope ceiling.

    Retried activities re-resolve current authority from this (never from a
    token or a workflow payload). The first record wins; recording again for
    the same run is a no-op.
    """

    async def record(self, run_id: str, principal: Principal) -> Principal: ...

    async def get(self, run_id: str) -> Principal | None: ...


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
    """Port to the execution runtime (today a Temporal workflow per run).

    Notifications carry identifiers only; the runtime reads persisted input.
    ``start`` is idempotent per run.
    """

    async def start(self, run_id: str) -> WorkflowRef: ...

    async def notify_input(self, run_id: str) -> None: ...

    async def request_cancel(self, run_id: str) -> None: ...
