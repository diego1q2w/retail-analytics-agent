"""Step use cases of the investigation runtime.

An execution runtime (the Temporal workflow, an adapter) runs these steps and
``investigation_lifecycle`` decides which step follows; this module decides
*whether* a step may happen and makes every step safe to repeat. Each method
may run inside a retryable activity and therefore:

- re-reads the run, its recorded principal and the executive's current
  authority (``AccessResolver.context_for_run``) instead of trusting anything
  carried in workflow state;
- checks the run is still running and its budget not spent *between* steps,
  not only when something is charged;
- writes through idempotent keys (message, question and operation IDs derived
  from the run and a step sequence), so a retried activity changes nothing
  twice;
- releases model-generated text only through ``OutputPrivacyGate`` with a
  policy built at release time, so evidence that became inaccessible while
  the model was working blocks the output (it is regenerated, never shown).

Provenance: every evidence record placed in the model's context, and every
record an answer cites, is linked to the run before the run's messages are
written, so conversation history can be judged against current authority
later (``HistoryRules``).

A final answer, a clarification question and the run-state change that
publishes it are written atomically with the check for newer user input
(``InvestigationInputs``): input that arrives while the model is working is
never dropped - the answer is superseded and the investigation continues with
it at the next safe boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
)
from retail_analytics.application.budgets import RunBudgets, budget_message
from retail_analytics.application.context import ContextBuilder, ModelContext
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.investigations import (
    AnswerDraft,
    AssistantOutput,
    BeginOutcome,
    CancelProgress,
    FinishRequest,
    ModelStep,
    QuestionDraft,
    StepOutcome,
    StepResult,
    StopReason,
)
from retail_analytics.application.contracts.progress import (
    EventKind,
    InputRequest,
    ProgressUpdate,
)
from retail_analytics.application.contracts.telemetry import (
    Label,
    Metric,
    ProviderAttribution,
    Span,
)
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.investigation_policy import (
    FETCH_EVIDENCE,
    render_investigation_policy,
)
from retail_analytics.application.investigations import InvestigationLauncher
from retail_analytics.application.output_privacy import (
    ACCESS_CHANGED,
    OutputDestination,
    OutputPrivacyGate,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.application.persona import PersonaDelivery
from retail_analytics.application.ports.investigations import (
    InvestigationInputs,
    RunPrincipals,
)
from retail_analytics.application.ports.persistence import (
    RunEventStore,
    RunRepository,
    ToolExecutionRepository,
)
from retail_analytics.application.query_execution import (
    QUERY_CAPABILITY,
    QueryCancelled,
    QueryExecutionService,
)
from retail_analytics.application.telemetry import telemetry
from retail_analytics.application.tools import (
    CapabilityRegistry,
    ExecutionContext,
    ToolDescriptor,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.budgets import BudgetResource, BudgetSnapshot
from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.evidence import EvidenceCell
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.investigations import (
    MAX_QUESTION_CHARS,
    ClarificationQuestion,
    InputKind,
    InputStatus,
    QuestionStatus,
    RunInput,
    answer_message_id,
    input_id_for,
    message_id_for,
    question_id_for,
)
from retail_analytics.domain.request_scope import Admission, AdmissionDecision
from retail_analytics.domain.runs import Run, RunStatus

_STOP_TIME = frozenset(
    {
        BudgetResource.ACTIVE_TIME,
        BudgetResource.PROVIDER_REQUESTS,
        BudgetResource.TOKENS,
    }
)
MAX_PARTIAL_EVIDENCE = 6
MAX_PARTIAL_ROWS = 5


_STOP_MESSAGES = {
    StopReason.CANCELLED: "The investigation was cancelled.",
    StopReason.ACCESS: (
        "Your access changed during the investigation, so it was stopped."
    ),
    StopReason.MODEL_UNAVAILABLE: (
        "The analysis service is unavailable right now; please try again later."
    ),
    StopReason.INTERRUPTED: "The investigation could not continue.",
}


class RunStopped(Exception):
    """No further model or tool work may start for this run."""

    def __init__(
        self, reason: StopReason, resource: BudgetResource | None = None
    ) -> None:
        self.reason = reason
        self.resource = resource
        super().__init__(f"run stopped: {reason.value}")


class InvestigationContextChanged(Exception):
    """The model conversation was built from context that is no longer valid.

    Raised by the agent integration before a provider sees the stale history;
    the investigation restarts the agent loop with rebuilt context (durable
    budgets and tool effects survive, derived claims do not).
    """

    def __init__(self) -> None:
        super().__init__("investigation context changed")


def _withheld(withheld: OutputWithheld) -> StepOutcome:
    if withheld.reason == ACCESS_CHANGED:
        # Evidence the run rests on is no longer readable: stop with the
        # access notice instead of asking the model to rephrase.
        return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.ACCESS)
    return StepOutcome(
        StepResult.WITHHELD,
        message=withheld.message,
        correctable=withheld.correctable,
    )


def stop_message(reason: StopReason, resource: BudgetResource | None) -> str:
    if reason is StopReason.BUDGET and resource is not None:
        return budget_message(resource)
    return _STOP_MESSAGES.get(reason, _STOP_MESSAGES[StopReason.INTERRUPTED])


INTERRUPTED_NOTICE = (
    "This investigation was interrupted because the analysis service stopped. "
    "It was not resumed and nothing was run again; earlier results and saved "
    "reports are kept. Send the request again to restart it."
)

INTERRUPTED_SUMMARY = (
    "Interrupted: the analysis service stopped. Nothing was resumed; send the "
    "request again."
)


def interruption_notice(*, unsettled: int, queued: int) -> str:
    """What the user is told when a run's execution was lost or shut down."""
    parts = [INTERRUPTED_NOTICE]
    if unsettled:
        parts.append(
            "A running query could not be confirmed stopped; it ends at its time limit."
        )
    if queued == 1:
        parts.append(
            "Your queued request was not started; send it again if you still need it."
        )
    elif queued:
        parts.append(
            f"Your {queued} queued requests were not started; send them again "
            "if you still need them."
        )
    return " ".join(parts)


def interruption_message_id(run_id: str) -> str:
    """The assistant message announcing the run's interruption (once)."""
    return message_id_for(input_id_for(run_id, "interrupted"))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InvestigationRuntime:
    def __init__(
        self,
        *,
        runs: RunRepository,
        principals: RunPrincipals,
        inputs: InvestigationInputs,
        resolver: AccessResolver,
        budgets: RunBudgets,
        context: ContextBuilder,
        gate: OutputPrivacyGate,
        registry: CapabilityRegistry,
        events: RunEventStore,
        evidence: EvidenceService,
        operations: ToolExecutionRepository,
        queries: QueryExecutionService | None,
        launcher: InvestigationLauncher,
        personas: PersonaDelivery | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._runs = runs
        self._principals = principals
        self._inputs = inputs
        self._resolver = resolver
        self._budgets = budgets
        self._context = context
        self._gate = gate
        self._registry = registry
        self._events = events
        self._evidence = evidence
        self._operations = operations
        self._queries = queries
        self._launcher = launcher
        self._personas = personas
        self._clock = clock

    async def begin(self, run_id: str) -> BeginOutcome:
        """Open the run's accounting and decide whether the request is in scope.

        Off-topic, unclear and topic-reset requests are handled without any
        model call (design: declined before spending model requests).
        """
        run = await self._runs.get_run(run_id)
        if run is None or run.status.is_terminal:
            return BeginOutcome(None if run is None else run.status)
        await self._budgets.open(run_id)
        await self._pinned_persona(run_id)
        await self._publish_once(run, EventKind.RUN_STARTED, "Investigation started.")
        if run.status is not RunStatus.RUNNING:
            return BeginOutcome(run.status, AdmissionDecision.PROCEED)
        principal = await self._principals.get(run_id)
        if principal is None:
            return BeginOutcome(run.status, AdmissionDecision.PROCEED)
        try:
            built = await self._build_context(principal, run)
        except AccessDenied:
            return BeginOutcome(run.status, AdmissionDecision.PROCEED)
        admission = built.admission
        _record_admission(run, admission)
        if admission.decision is AdmissionDecision.RESET_TOPIC:
            await self._context.reset_topic(principal, run.session_id, run.run_id)
        return BeginOutcome(run.status, admission.decision, admission.message)

    async def prepare_model_step(self, run_id: str) -> ModelStep:
        """Context and tools for the next model request, or ``RunStopped``.

        Applies pending steering/answers (the safe boundary), assembles the
        context under current authority and links the evidence it shows to
        the run.
        """
        principal, run = await self._running(run_id)
        snapshot = await self._budgets.snapshot(run_id)
        _check_budget(snapshot)
        context = await self._context_for(principal, run_id)
        await self._inputs.apply_pending(run_id)
        try:
            built = await self._build_context(principal, run)
        except AccessDenied:
            raise RunStopped(StopReason.ACCESS) from None
        await self._evidence.link_to_run(
            context, [digest.evidence_id for digest in built.evidence]
        )
        tools = frozenset(d.name for d in self._registry.catalog(context))
        persona = await self._pinned_persona(run_id)
        return ModelStep(
            instructions="\n".join(
                [
                    render_investigation_policy(tools),
                    *([persona] if persona else []),
                    _budget_line(snapshot),
                    built.render(can_fetch_evidence=FETCH_EVIDENCE in tools),
                ]
            ),
            tools=tools,
            history_key=hashlib.sha256(
                json.dumps(
                    [
                        run_id,
                        built.authorization_version,
                        built.request,
                        built.preferences,
                        str(built.topic_reset_at),
                        [(m.role.value, m.text) for m in built.history],
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            evidence_versions=tuple(
                (digest.evidence_id, digest.version) for digest in built.evidence
            ),
        )

    async def catalog(self, run_id: str) -> tuple[ToolDescriptor, ...]:
        """The tools the run may use now (current authority)."""
        principal = await self._principals.get(run_id)
        if principal is None:
            return ()
        try:
            context = await self._resolver.context_for_run(
                principal, run_id, trace_id=run_id
            )
        except AccessDenied:
            return ()
        return self._registry.catalog(context)

    async def release_answer(self, draft: AnswerDraft) -> StepOutcome:
        """Release the final answer, or say why not.

        The gate runs with a policy built now; cited evidence is linked to
        the run, then the answer and the run's completion are written
        together with the check for newer input.
        """
        run = await self._runs.get_run(draft.run_id)
        if run is None:
            return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.ACCESS)
        status = RunStatus.COMPLETED if draft.complete else RunStatus.PARTIAL
        if run.status.is_terminal:
            # A retry after the answer was closed as partial (it cited an
            # incomplete result) is the same release.
            if draft.complete and run.status is RunStatus.PARTIAL:
                status = RunStatus.PARTIAL
            return await self._after_close(run, status, retried=True)
        if run.status is RunStatus.CANCELLING:
            return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.CANCELLED)
        if await self._inputs.pending(draft.run_id):
            return StepOutcome(StepResult.SUPERSEDED)
        principal = await self._principals.get(draft.run_id)
        if principal is None:
            return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.ACCESS)
        try:
            context = await self._context_for(principal, draft.run_id)
            (released,) = await self._gate.check(
                principal,
                draft.run_id,
                [OutputSection("answer", draft.text, draft.cited_evidence)],
                OutputDestination.DISPLAY,
                trace_id=draft.run_id,
            )
        except RunStopped as stopped:
            return StepOutcome(StepResult.STOPPED, stop_reason=stopped.reason)
        except AccessDenied:
            return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.ACCESS)
        except OutputWithheld as withheld:
            return _withheld(withheld)
        await self._evidence.link_to_run(context, released.cited_evidence)
        cited = await self._cited(context, draft.run_id, released.cited_evidence)
        if status is RunStatus.COMPLETED and any(
            s.evidence.content.table.truncated for s in cited
        ):
            # Never record an answer resting on a cut result as complete.
            status = RunStatus.PARTIAL
        closure = await self._inputs.close_run(
            draft.run_id,
            status,
            output=AssistantOutput(
                answer_message_id(draft.run_id, draft.sequence),
                released.text + _source_notes(cited),
            ),
        )
        if not closure.closed:
            if closure.run.status.is_terminal:
                return StepOutcome(
                    StepResult.STOPPED,
                    status=closure.run.status,
                    stop_reason=StopReason.CANCELLED,
                )
            return StepOutcome(StepResult.SUPERSEDED)
        return await self._after_close(
            closure.run, status, retried=False, served_by=draft.served_by
        )

    async def ask(self, draft: QuestionDraft) -> StepOutcome:
        """Pause for a clarification; waiting costs no model calls or time."""
        question_id = question_id_for(draft.run_id, draft.sequence)
        open_question = await self._inputs.open_question(draft.run_id)
        recovering = (
            open_question is not None and open_question.question_id == question_id
        )
        try:
            if recovering:
                principal = await self._principals.get(draft.run_id)
                run = await self._runs.get_run(draft.run_id)
                if principal is None or run is None:
                    raise RunStopped(StopReason.ACCESS)
                if run.status is not RunStatus.WAITING_FOR_INPUT:
                    raise RunStopped(StopReason.CANCELLED)
                await self._context_for(principal, draft.run_id)
            else:
                principal, run = await self._running(draft.run_id)
        except RunStopped as stopped:
            return StepOutcome(StepResult.STOPPED, stop_reason=stopped.reason)
        if not recovering and await self._inputs.pending(draft.run_id):
            return StepOutcome(StepResult.SUPERSEDED)
        try:
            (released,) = await self._gate.check(
                principal,
                draft.run_id,
                [OutputSection("question", draft.question[:MAX_QUESTION_CHARS])],
                OutputDestination.PROGRESS,
                trace_id=draft.run_id,
            )
        except AccessDenied:
            return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.ACCESS)
        except OutputWithheld as withheld:
            return _withheld(withheld)
        if not recovering:
            waiting = await self._inputs.wait_for_input(
                ClarificationQuestion(
                    question_id=question_id,
                    run_id=draft.run_id,
                    message_id=message_id_for(question_id),
                    status=QuestionStatus.OPEN,
                    asked_at=self._clock(),
                ),
                content=released.text,
            )
            if not waiting:
                return StepOutcome(StepResult.SUPERSEDED)
        await self._budgets.pause_for_clarification(draft.run_id)
        await self._publish_once(
            run,
            EventKind.INPUT_REQUIRED,
            "Waiting for your answer.",
            input_request=InputRequest(
                question_id=question_id, question=released.text[:MAX_QUESTION_CHARS]
            ),
        )
        return StepOutcome(StepResult.ASKED, question_id=question_id)

    async def resume(self, run_id: str) -> StepOutcome:
        """Continue a waiting run once input has been recorded."""
        run = await self._runs.get_run(run_id)
        if run is None or run.status.is_terminal or run.status is RunStatus.CANCELLING:
            return StepOutcome(StepResult.STOPPED, stop_reason=StopReason.CANCELLED)
        if not await self._inputs.resume_with_input(run_id):
            return StepOutcome(StepResult.IDLE)
        await self._budgets.resume_after_clarification(run_id)
        return StepOutcome(StepResult.CONTINUE)

    async def finish_partial(self, request: FinishRequest) -> StepOutcome:
        """End a run that cannot continue, showing verified findings so far.

        Built from evidence the run produced or used - no model call - so it
        works when the model budget is spent. The text passes the output gate
        like any answer.
        """
        run = await self._runs.get_run(request.run_id)
        if run is None:
            return StepOutcome(StepResult.STOPPED, stop_reason=request.reason)
        if run.status.is_terminal:
            return await self._after_close(run, run.status, retried=True)
        reason = stop_message(request.reason, request.resource)
        telemetry().count(
            Metric.BUDGET_STOPS,
            {
                Label.REASON: request.reason.value,
                Label.RESOURCE: request.resource.value if request.resource else "none",
            },
        )
        sections = await self._partial_findings(request.run_id, reason)
        status = RunStatus.PARTIAL if sections.cited else RunStatus.FAILED
        closure = await self._inputs.close_run(
            request.run_id,
            status,
            output=AssistantOutput(answer_message_id(request.run_id, 0), sections.text),
            force=True,
        )
        if not closure.closed:
            return StepOutcome(StepResult.STOPPED, status=closure.run.status)
        return await self._after_close(closure.run, status, retried=False)

    async def finish_message(self, run_id: str, message: str) -> StepOutcome:
        """End the run with an application-authored message (no model call)."""
        run = await self._runs.get_run(run_id)
        if run is None:
            return StepOutcome(StepResult.STOPPED)
        if run.status.is_terminal:
            return await self._after_close(run, run.status, retried=True)
        closure = await self._inputs.close_run(
            run_id,
            RunStatus.COMPLETED,
            output=AssistantOutput(answer_message_id(run_id, 0), message),
            force=True,
        )
        return await self._after_close(closure.run, RunStatus.COMPLETED, retried=False)

    async def expire(self, run_id: str) -> StepOutcome:
        """Waiting ended without an answer within the retention window."""
        run = await self._runs.get_run(run_id)
        if run is None or run.status.is_terminal:
            return StepOutcome(StepResult.STOPPED)
        closure = await self._inputs.close_run(run_id, RunStatus.FAILED)
        if not closure.closed:
            return StepOutcome(StepResult.SUPERSEDED)
        return await self._after_close(closure.run, RunStatus.FAILED, retried=False)

    async def begin_cancel(self, run_id: str) -> CancelProgress:
        """Stop new work and request cancellation of running operations."""
        run = await self._runs.get_run(run_id)
        if run is None or run.status.is_terminal:
            return CancelProgress(0)
        if run.status is not RunStatus.CANCELLING:
            await self._runs.transition_run(run_id, RunStatus.CANCELLING)
        return CancelProgress(await self._cancel_operations(run_id))

    async def interrupt(self, run_id: str) -> StepOutcome:
        """End a run whose execution is gone without resuming or replaying it.

        For an execution runtime without durable recovery (the in-process
        local manager) when its process stops, or after it died. No model
        call and no tool is run again: running external operations only get
        cancellation requested through their recorded job references, and an
        unconfirmed one is reported as such. The run ends FAILED (CANCELLED
        when cancellation was already requested) with a notice; pending
        steering/answers and the session's queued requests are discarded,
        kept as history and named in the notice, and nothing queued is
        started: the user sends a new request. Earlier messages, evidence,
        reports, budgets and operation records are kept.
        """
        run = await self._runs.get_run(run_id)
        if run is None or run.status.is_terminal:
            return StepOutcome(
                StepResult.STOPPED, status=None if run is None else run.status
            )
        unsettled = await self._cancel_operations(run_id)
        queued = await self._discard_queued(run.session_id)
        status = (
            RunStatus.CANCELLED
            if run.status is RunStatus.CANCELLING
            else RunStatus.FAILED
        )
        notice = interruption_notice(unsettled=unsettled, queued=queued)
        closure = await self._inputs.close_run(
            run_id,
            status,
            output=AssistantOutput(interruption_message_id(run_id), notice),
            force=True,
        )
        await self._inputs.discard_pending(run_id)
        if not closure.closed:
            # It ended meanwhile (its own outcome stands).
            return StepOutcome(StepResult.STOPPED, status=closure.run.status)
        await self._record_run_end(closure.run, None)
        kind = (
            EventKind.RUN_CANCELLED
            if status is RunStatus.CANCELLED
            else EventKind.RUN_FAILED
        )
        await self._publish_once(closure.run, kind, INTERRUPTED_SUMMARY)
        return StepOutcome(
            StepResult.STOPPED,
            status=closure.run.status,
            stop_reason=StopReason.INTERRUPTED,
        )

    async def _cancel_operations(self, run_id: str) -> int:
        """Request cancellation of the run's unfinished operations.

        Returns how many may still leave an external effect (cancellation not
        confirmed). Queries are cancelled through their recorded job
        reference, never resubmitted.
        """
        unsettled = 0
        for op in await self._operations.for_run(run_id):
            if op.status.is_terminal:
                continue
            if op.side_effect.may_leave_external_effect:
                if self._queries is None or op.capability != QUERY_CAPABILITY:
                    unsettled += 1
                    continue
                outcome = await self._queries.cancel(run_id, op.operation_id)
                if not (isinstance(outcome, QueryCancelled) and outcome.confirmed):
                    current = await self._operations.get(op.operation_id)
                    if current is not None and not current.status.is_terminal:
                        unsettled += 1
            else:
                await self._operations.transition(
                    op.operation_id,
                    ToolExecutionStatus.CANCELLED,
                    attempt=max(op.attempt_count, 1),
                    detail="cancelled",
                )
        return unsettled

    async def _discard_queued(self, session_id: str) -> int:
        discarded = 0
        while (queued := await self._inputs.next_queued(session_id)) is not None:
            await self._inputs.discard(queued.input_id)
            discarded += 1
        return discarded

    async def reconcile_cancel(self, run_id: str) -> CancelProgress:
        unsettled = 0
        for op in await self._operations.for_run(run_id):
            if op.status.is_terminal:
                continue
            if op.status is ToolExecutionStatus.CANCEL_REQUESTED and self._queries:
                await self._queries.reconcile_cancel(run_id, op.operation_id)
            current = await self._operations.get(op.operation_id)
            if current is not None and not current.status.is_terminal:
                unsettled += 1
        return CancelProgress(unsettled)

    async def finish_cancelled(self, run_id: str, *, settled: bool) -> StepOutcome:
        run = await self._runs.get_run(run_id)
        if run is None:
            return StepOutcome(StepResult.STOPPED)
        if not run.status.is_terminal:
            run = (
                await self._inputs.close_run(run_id, RunStatus.CANCELLED, force=True)
            ).run
            await self._record_run_end(run, None)
        summary = (
            "Cancelled. Work that already completed is kept."
            if settled
            else "Cancelled. A running query could not be confirmed stopped yet; "
            "it ends at its time limit."
        )
        await self._inputs.discard_pending(run_id)
        await self._publish_once(run, EventKind.RUN_CANCELLED, summary)
        await self._launcher.promote_next(run.session_id)
        return StepOutcome(StepResult.STOPPED, status=run.status)

    async def _pinned_persona(self, run_id: str) -> str | None:
        """The run's persona block: pinned when the run starts, then fixed.

        It supplies presentation defaults only and sits between the policy and
        the per-run context, so the policy and the user's preferences stay in
        force. A persona cannot add tools or change authority: the catalog and
        every check come from code.
        """
        if self._personas is None:
            return None
        return await self._personas.section_for_run(run_id)

    async def _running(self, run_id: str) -> tuple[Principal, Run]:
        principal = await self._principals.get(run_id)
        run = await self._runs.get_run(run_id)
        if principal is None or run is None:
            raise RunStopped(StopReason.ACCESS)
        if run.status is not RunStatus.RUNNING:
            raise RunStopped(StopReason.CANCELLED)
        return principal, run

    async def _context_for(self, principal: Principal, run_id: str) -> ExecutionContext:
        try:
            context = await self._resolver.context_for_run(
                principal, run_id, trace_id=run_id
            )
        except AccessDenied:
            raise RunStopped(StopReason.ACCESS) from None
        if Permission.ANALYSIS_READ.value not in context.permissions:
            raise RunStopped(StopReason.ACCESS)
        return context

    async def _cited(
        self, context: ExecutionContext, run_id: str, cited: Sequence[str]
    ) -> list[EvidenceStanding]:
        if not cited:
            return []
        session = await self._evidence.session_standing(context, run_ids=[run_id])
        wanted = set(cited)
        return [s for s in session.standings if s.evidence.evidence_id in wanted]

    async def _build_context(self, principal: Principal, run: Run) -> ModelContext:
        inputs = await self._inputs.for_run(run.run_id)
        return await self._context.build(
            principal,
            run.run_id,
            compose_request(inputs),
            request_message_id=run.trigger_message_id,
            trace_id=run.run_id,
        )

    async def _after_close(
        self,
        run: Run,
        status: RunStatus,
        *,
        retried: bool,
        served_by: ProviderAttribution | None = None,
    ) -> StepOutcome:
        if run.status is not status and retried:
            return StepOutcome(StepResult.STOPPED, status=run.status)
        if not retried:
            await self._record_run_end(run, served_by)
        await self._inputs.discard_pending(run.run_id)
        kind, summary = _TERMINAL_EVENTS[run.status]
        await self._publish_once(run, kind, summary)
        await self._launcher.promote_next(run.session_id)
        return StepOutcome(StepResult.RELEASED, status=run.status)

    async def _record_run_end(
        self, run: Run, served_by: ProviderAttribution | None
    ) -> None:
        """Run outcome, duration, budget use and answering provider, once."""
        now = self._clock()
        status = run.status.value
        seconds = max((now - run.created_at).total_seconds(), 0.0)
        attributes: dict[str, object] = {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "status": status,
        }
        if served_by is not None:
            attributes.update(
                answered_by=served_by.provider,
                answered_model=served_by.model,
                fallback_from=served_by.fallback_from or "none",
                fallback_reason=served_by.fallback_reason or "none",
            )
        snapshot = await self._budgets.snapshot(run.run_id)
        if snapshot is not None:
            for resource, left in snapshot.remaining().items():
                limit = _limit_of(snapshot, resource)
                if limit > 0:
                    used = min(max(1 - left / limit, 0.0), 1.0)
                    attributes[f"budget_used_{resource.value}"] = round(used, 3)
                    telemetry().observe(
                        Metric.RUN_BUDGET_USE,
                        used,
                        {Label.RESOURCE: resource.value},
                    )
        with telemetry().span(
            Span.RUN,
            run_id=run.run_id,
            attributes=attributes,
            start=run.created_at,
            root=True,
        ):
            pass
        telemetry().count(Metric.RUNS, {Label.STATUS: status})
        telemetry().observe(Metric.RUN_SECONDS, seconds, {Label.STATUS: status})
        if served_by is not None:
            telemetry().count(
                Metric.FINAL_ANSWERS,
                {
                    Label.PROVIDER: served_by.provider,
                    Label.MODEL: served_by.model,
                    Label.OUTCOME: "fallback" if served_by.fallback_from else "primary",
                },
            )

    async def _partial_findings(self, run_id: str, reason: str) -> _Partial:
        principal = await self._principals.get(run_id)
        standings: Sequence[EvidenceStanding] = ()
        if principal is not None:
            try:
                context = await self._context_for(principal, run_id)
                session = await self._evidence.session_standing(
                    context, run_ids=[run_id]
                )
                linked = session.run_links.get(run_id, frozenset())
                standings = [
                    s
                    for s in session.standings
                    if s.usable and s.evidence.evidence_id in linked
                ][:MAX_PARTIAL_EVIDENCE]
            except (RunStopped, AccessDenied):
                standings = ()
        for detail in (True, False):
            text, cited = _partial_text(reason, standings, rows=detail)
            if principal is None:
                break
            try:
                (released,) = await self._gate.check(
                    principal,
                    run_id,
                    [OutputSection("answer", text, cited)],
                    OutputDestination.DISPLAY,
                    trace_id=run_id,
                )
                return _Partial(released.text, cited)
            except (OutputWithheld, AccessDenied):
                continue
        return _Partial(f"{reason} No verified findings can be shown.", ())

    async def _publish_once(
        self,
        run: Run,
        kind: EventKind,
        summary: str,
        *,
        input_request: InputRequest | None = None,
    ) -> None:
        for event in await self._events.replay(run.run_id, limit=10_000):
            if event.kind is kind and (
                input_request is None or event.input_request == input_request
            ):
                return
        await self._events.publish(
            ProgressUpdate(
                correlation=Correlation(
                    session_id=run.session_id, run_id=run.run_id, trace_id=run.run_id
                ),
                kind=kind,
                summary=summary,
                input_request=input_request,
            )
        )


@dataclass(frozen=True, slots=True)
class _Partial:
    text: str
    cited: tuple[str, ...]


_TERMINAL_EVENTS: dict[RunStatus, tuple[EventKind, str]] = {
    RunStatus.COMPLETED: (EventKind.RUN_COMPLETED, "Answer ready."),
    RunStatus.PARTIAL: (
        EventKind.RUN_PARTIAL,
        "Partial answer ready: some of the work could not be completed.",
    ),
    RunStatus.FAILED: (
        EventKind.RUN_FAILED,
        "The investigation ended without an answer.",
    ),
    RunStatus.CANCELLED: (EventKind.RUN_CANCELLED, "Cancelled."),
}


def compose_request(inputs: Sequence[RunInput]) -> str:
    """The request as the model should read it: the original question, then
    every applied later message in order (later ones win on conflict)."""
    parts: list[str] = []
    for item in inputs:
        if item.status is not InputStatus.APPLIED:
            continue
        match item.kind:
            case InputKind.REQUEST:
                parts.insert(0, item.content)
            case InputKind.STEERING:
                parts.append(
                    "[Later message from the user; it overrides earlier "
                    f"assumptions where they conflict] {item.content}"
                )
            case InputKind.ANSWER:
                parts.append(f"[The user's answer to your question] {item.content}")
            case _:
                continue
    return "\n\n".join(parts) if parts else "(no request text)"


def _limit_of(snapshot: BudgetSnapshot, resource: BudgetResource) -> float:
    limits = snapshot.limits
    return float(
        {
            BudgetResource.ACTIVE_TIME: limits.active_seconds,
            BudgetResource.PROVIDER_REQUESTS: limits.provider_requests,
            BudgetResource.TOKENS: limits.tokens,
            BudgetResource.QUERIES: limits.queries,
            BudgetResource.RUN_BYTES: limits.bytes_per_run,
        }.get(resource, 0)
    )


def _check_budget(snapshot: BudgetSnapshot | None) -> None:
    if snapshot is None:
        return
    spent = snapshot.exhausted() & _STOP_TIME
    if spent:
        raise RunStopped(StopReason.BUDGET, sorted(spent)[0])


def _budget_line(snapshot: BudgetSnapshot | None) -> str:
    if snapshot is None:
        return "<budget>unknown</budget>"
    left = snapshot.remaining()
    return (
        "<budget>"
        f"model requests left: {int(left[BudgetResource.PROVIDER_REQUESTS])}; "
        f"queries left: {int(left[BudgetResource.QUERIES])}; "
        f"active seconds left: {int(left[BudgetResource.ACTIVE_TIME])}"
        "</budget>"
    )


def _cell(value: EvidenceCell) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _partial_text(
    reason: str, standings: Sequence[EvidenceStanding], *, rows: bool
) -> tuple[str, tuple[str, ...]]:
    if not standings:
        return f"{reason} No verified findings were produced yet.", ()
    lines = [f"{reason} Verified findings so far:"]
    cited: list[str] = []
    for standing in standings:
        evidence = standing.evidence
        table = evidence.content.table
        incomplete = " (incomplete result)" if table.truncated else ""
        lines.append(
            f"- Evidence {evidence.evidence_id}: {len(table.rows)} rows of "
            f"{', '.join(table.column_names)}{incomplete}."
        )
        if rows:
            for row in table.rows[:MAX_PARTIAL_ROWS]:
                lines.append(
                    "  "
                    + "; ".join(
                        f"{name} = {_cell(cell)}"
                        for name, cell in zip(table.column_names, row, strict=True)
                    )
                )
        cited.append(evidence.evidence_id)
    lines.append("The remaining work was not completed.")
    return "\n".join(lines), tuple(cited)


def _source_notes(cited: Sequence[EvidenceStanding]) -> str:
    """Application-authored provenance for saved-report figures in an answer:
    reused figures always keep their source and date, never read as current."""
    lines = [
        f"- {s.evidence.evidence_id}: {s.source.describe(s.evidence)}"
        for s in cited
        if s.source is not None
    ]
    return "\n\nSources from saved reports:\n" + "\n".join(lines) if lines else ""


def _record_admission(run: Run, admission: Admission) -> None:
    """Codes only: why the request was admitted, declined or clarified."""
    with telemetry().span(
        Span.ADMISSION,
        run_id=run.run_id,
        attributes={
            "run_id": run.run_id,
            "session_id": run.session_id,
            "decision": admission.decision.value,
            "topic": admission.topic.value,
            "reason": admission.reason.value if admission.reason else "unknown",
            "classifier_version": admission.classifier_version,
        },
    ):
        pass
