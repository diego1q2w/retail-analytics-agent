from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.telemetry import ProviderAttribution
from retail_analytics.domain.budgets import BudgetResource
from retail_analytics.domain.investigations import RunInput
from retail_analytics.domain.request_scope import AdmissionDecision
from retail_analytics.domain.runs import (
    Run,
    RunStatus,
)


@dataclass(frozen=True)
class RecoveryCandidate:
    run_id: str
    session_id: str
    principal: Principal
    request_text: str
    submission_key: str
    status: RunStatus


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
    # Steering/answers a forced close ended unapplied (with a notice), in
    # arrival order; empty otherwise.
    unapplied: tuple[RunInput, ...] = ()


class StopReason(StrEnum):
    BUDGET = "budget"
    CANCELLED = "cancelled"
    ACCESS = "access"
    # The model provider failed in a way retries could not recover.
    MODEL_UNAVAILABLE = "model_unavailable"
    INTERRUPTED = "interrupted"


# Payloads exchanged between an execution runtime (the Temporal workflow) and
# the investigation runtime use cases. Small; text only where unavoidable.
# Their field names and enum values are serialized in durable runtime history:
# changing them breaks replay of investigations already running.


@dataclass(frozen=True, slots=True)
class ContextStanding:
    """What is valid now, judged under current authority, beyond the bounded
    selection one request shows. An earlier model response stays reusable
    only while everything it was shown is still listed here unchanged;
    anything missing (invalidated, scope lost, withdrawn, unknown) is not.
    """

    # Every usable session record (after the latest reset) and its version.
    evidence: tuple[tuple[str, int], ...]
    # Every history message that may still enter context, by fingerprint.
    messages: tuple[tuple[str, str], ...]
    # Digest per part of ``ModelStep.history_key`` (names a restart cause).
    key_parts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ToolFocus:
    """Which analytical skills one model request has in effect.

    A relevance choice inside the authorized catalog, never an authority
    decision: it only ever removes authorized tools from view
    (``application.tool_focus``).
    """

    # (skill id, pinned version) of every skill in effect.
    active: tuple[tuple[str, int], ...] = ()
    # Authorized skills that can still be loaded.
    loadable: tuple[str, ...] = ()
    # Tools the principal may use now (the authorized catalog) and exposed.
    authorized: int = 0
    exposed: int = 0


@dataclass(frozen=True, slots=True)
class ModelStep:
    """What one model request may see and use, built under current authority.

    Built inside the model request (activity), never part of durable history.
    """

    instructions: str
    tools: frozenset[str]
    # Authority, request, preferences and topic reset; never prompt capacity.
    history_key: str
    # Evidence shown to this request.
    evidence_versions: tuple[tuple[str, int], ...]
    # History messages shown to this request, by fingerprint.
    history_messages: tuple[tuple[str, str], ...] = ()
    # None: nothing beyond this request's selection is known to be valid.
    standing: ContextStanding | None = None
    # Analytical skills in effect for this request.
    focus: ToolFocus | None = None
    # Active seconds the run had left when this step was prepared (None: no
    # accounting); the model request is cut off when they run out.
    active_seconds_left: float | None = None


class ContextKeyPart(StrEnum):
    """The parts of ``ModelStep.history_key``: a change to any of them
    invalidates the whole conversation."""

    AUTHORITY = "authority"
    REQUEST = "request"
    PREFERENCES = "preferences"
    TOPIC_RESET = "topic_reset"
    # The approved schema the context described (catalog, availability).
    SCHEMA = "schema"


class ContextRestartCause(StrEnum):
    """Why a model conversation was restarted (telemetry code, never content)."""

    PROVENANCE_MISSING = "provenance_missing"
    AUTHORITY_CHANGED = "authority_changed"
    REQUEST_CHANGED = "request_changed"
    PREFERENCES_CHANGED = "preferences_changed"
    TOPIC_RESET = "topic_reset"
    SCHEMA_CHANGED = "schema_changed"
    CONTEXT_CHANGED = "context_changed"
    HISTORY_CHANGED = "history_changed"
    EVIDENCE_INVALIDATED = "evidence_invalidated"


@dataclass(frozen=True, slots=True)
class BeginOutcome:
    status: RunStatus | None
    # PROCEED: run the agent; DECLINE/RESET_TOPIC: finish with ``message``;
    # CLARIFY: ask ``message``. None when the run is missing or ended.
    admission: AdmissionDecision | None = None
    message: str | None = None


class Restriction(StrEnum):
    """An application-enforced restriction under which a part of a request
    is declined (with an alternative offered) rather than left unanswered."""

    # One customer's demographics or profile (demographics are group-level only).
    INDIVIDUAL_DEMOGRAPHICS = "individual_demographics"
    # Data outside the executive's permitted products or brands.
    OUTSIDE_PERMITTED_SCOPE = "outside_permitted_scope"


@dataclass(frozen=True, slots=True)
class AnswerDraft:
    run_id: str
    # Distinct per release attempt within the run (idempotency of the message).
    sequence: int
    text: str
    cited_evidence: tuple[str, ...] = ()
    # The model's claim that no permitted requested work is left unanswered.
    complete: bool = True
    # Which provider produced the answer; for telemetry only, never shown.
    served_by: ProviderAttribution | None = None
    # Restrictions under which the model declined part of the request. A
    # declined part counts as resolved only when the application confirms the
    # restriction (``application.answer_completion``).
    declined: tuple[Restriction, ...] = ()


@dataclass(frozen=True, slots=True)
class QuestionDraft:
    run_id: str
    sequence: int
    question: str


class StepResult(StrEnum):
    RELEASED = "released"
    # The output may not be released; regenerate (``message`` says why).
    WITHHELD = "withheld"
    # Newer user input arrived; continue the investigation with it.
    SUPERSEDED = "superseded"
    STOPPED = "stopped"
    ASKED = "asked"
    CONTINUE = "continue"
    # Nothing to do yet (e.g. a wake-up without new input).
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class StepOutcome:
    result: StepResult
    message: str | None = None
    correctable: bool = False
    question_id: str | None = None
    status: RunStatus | None = None
    stop_reason: StopReason | None = None


@dataclass(frozen=True, slots=True)
class CancelProgress:
    # Operations whose cancellation is not yet confirmed.
    unsettled: int


@dataclass(frozen=True, slots=True)
class FinishRequest:
    run_id: str
    reason: StopReason
    resource: BudgetResource | None = None


# Lifecycle decisions (``application.investigation_lifecycle``): what an
# execution runtime does next. Runtimes interpret them with their own
# primitives (activities, durable timers, signals); they are never persisted.


class InterruptionKind(StrEnum):
    # The conversation's source context became invalid; restart the agent loop.
    CONTEXT_CHANGED = "context_changed"
    # The runtime refused further work (``RunStopped``): budget, access, ...
    STOPPED = "stopped"
    # The agent loop failed for any other reason (provider, usage limit).
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentInterruption:
    """Why an agent run ended without an answer or a question."""

    kind: InterruptionKind
    reason: StopReason | None = None
    resource: BudgetResource | None = None


class LifecycleAction(StrEnum):
    # Run the agent with context rebuilt under current authority.
    INVESTIGATE = "investigate"
    # Check persisted input; wait for a notification if there is none.
    AWAIT_INPUT = "await_input"
    # Nothing to do until new input is notified (bounded by ``WAIT_LIMIT``).
    WAIT = "wait"
    # Publish ``message`` and close the run without a model call.
    FINISH_MESSAGE = "finish_message"
    # Ask ``message`` as the run's clarification question.
    ASK = "ask"
    # The run is done (answered, missing or already ended).
    CLOSE = "close"
    # End with partial findings for ``stop_reason`` (and budget ``resource``).
    STOP = "stop"
    # Cooperative cancellation: stop new work, reconcile in-flight effects.
    CANCEL = "cancel"
    # The clarification wait expired.
    EXPIRE = "expire"


@dataclass(frozen=True, slots=True)
class LifecycleDecision:
    action: LifecycleAction
    message: str | None = None
    stop_reason: StopReason | None = None
    resource: BudgetResource | None = None
    # Set by admission when the run is already being cancelled.
    cancelling: bool = False


# Interrupted local executions (``application.investigation_interruption``).


@dataclass(frozen=True, slots=True)
class InterruptionSweep:
    """What a startup sweep of orphaned local runs changed."""

    # Runs that ended interrupted (FAILED, or CANCELLED if cancelling).
    interrupted: tuple[str, ...] = ()
    # Queued requests discarded with a notice instead of being started.
    discarded_queued: int = 0
