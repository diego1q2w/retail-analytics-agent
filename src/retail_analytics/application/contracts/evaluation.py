from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import (
    Annotated,
    Any,
    Literal,
)

from pydantic import Field

from retail_analytics.application.contracts import (
    ContractModel,
    Identifier,
)

Mode = Literal["fixture", "live"]


Scalar = bool | int | float | str | None


class ScopeSpec(ContractModel):
    """Who is asking and what they may see (synthetic references only)."""

    executive_ref: Identifier
    product_scope: tuple[Identifier, ...] = ()


class Turn(ContractModel):
    """One user message. Agent turns are produced by the target, not authored."""

    text: Annotated[str, Field(min_length=1, max_length=8000)]


class TargetUnavailable(Exception):
    """The target cannot run this case now (credentials, provider, no recording).

    Mapped to a blocked case, never to a pass or a failure of the agent.
    """


class ScenarioInput(ContractModel):
    """What a target sees: no expectations, so it cannot grade itself."""

    scenario_id: Identifier
    mode: Mode
    fixture_ref: Identifier | None
    scope: ScopeSpec
    dialogue: tuple[Turn, ...]


class TargetObservation(ContractModel):
    """What a target reports. Only digests and numbers reach the result file.

    ``values`` holds named intermediate and final results (check names refer to
    them); ``measurements`` holds operational numbers such as latency, model
    usage, query count and bytes processed.
    """

    answer_text: str = ""
    values: Mapping[Identifier, Scalar] = {}
    tool_calls: tuple[Identifier, ...] = ()
    measurements: Mapping[Identifier, float] = {}


class JudgeScoreOut(ContractModel):
    dimension: Identifier
    score: float
    evidence_refs: tuple[Identifier, ...] = ()


@dataclass(frozen=True)
class EngineResult:
    row: Mapping[str, Any]
    bytes_processed: int = 0
    bytes_billed: int = 0
    job_id: str = ""


# Agent-runtime evaluation: what a target recorded for one conversation, read
# back from durable records after the runs ended (never from model claims).


@dataclass(frozen=True)
class ObservedTable:
    """One evidence table produced or used by the conversation's runs."""

    evidence_id: str
    columns: tuple[str, ...]
    # Privacy role per column ("reference", "age_band" or "value").
    roles: tuple[str, ...]
    # Logical source fields per column ("relation.field").
    sources: tuple[tuple[str, ...], ...]
    rows: tuple[tuple[Scalar, ...], ...]
    truncated: bool
    # The evidence was computed under exactly the provisioned product scope.
    scope_matches: bool


@dataclass(frozen=True)
class ConversationRecord:
    """Released output and durable records of one evaluated conversation."""

    answers: tuple[str, ...]
    tables: tuple[ObservedTable, ...]
    # Capability names the model invoked, in order (including refused ones).
    tool_calls: tuple[str, ...]
    # Markdown of saved reports and their action-item counts.
    report_texts: tuple[str, ...] = ()
    report_actions: int = 0
    report_evidence: int = 0
    measurements: Mapping[str, float] = field(default_factory=dict)
    # The user's own messages: repeating a name the user typed is not a leak.
    user_texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioCanaries:
    """Values that must never be released, derived from the fixture and scope."""

    personal_strings: frozenset[str] = frozenset()
    raw_customer_ids: frozenset[str] = frozenset()
    exact_ages: frozenset[int] = frozenset()
    out_of_scope_products: frozenset[str] = frozenset()
    out_of_scope_product_ids: frozenset[str] = frozenset()
    # Whole-order totals that include items outside the scope.
    full_basket_totals: frozenset[float] = frozenset()


# Real-model evaluation (T37): what telemetry recorded about one span. Only the
# already-sanitized identifiers and codes of the telemetry facade.

SpanValue = str | int | float | bool


@dataclass(frozen=True)
class RecordedSpan:
    """A finished telemetry span, as an in-process recorder saw it."""

    name: str
    run_id: str | None
    attributes: Mapping[str, SpanValue]
    # Sanitized captured content by side ("inputs"/"outputs"), kept only for
    # the spans a recorder is asked to keep (for example the compiler's).
    content: Mapping[str, object] = field(default_factory=dict)


# Real-model evaluation results (T37). Identifiers, codes, numbers and the
# expected figures of public manifests only; no answer text.

ProviderLabel = Literal["primary", "fallback", "mixed", "unattributed"]


class ProviderUse(ContractModel):
    """Which providers served one conversation, from telemetry spans.

    ``primary``: every answered run and every successful request came from the
    primary provider. ``fallback``: the primary served nothing. ``mixed``: both
    served requests. ``unattributed``: nothing was recorded (e.g. no model
    request completed).
    """

    label: ProviderLabel
    # One entry per run that released a model answer, in run order.
    answered_by: tuple[str, ...] = ()
    # "provider:model:outcome" -> request count.
    attempts: Mapping[str, int] = {}
    fallback_reasons: tuple[str, ...] = ()


class FigureCheck(ContractModel):
    """One independently computed expected figure, checked twice.

    ``in_evidence``: some released evidence cell matches (whatever its column
    name); null for labels with no evidence cell to compare. ``in_answer``: the
    released answers or saved reports state it.
    """

    name: Identifier
    kind: Literal["number", "label"]
    expected: float | None = None
    in_evidence: bool | None = None
    in_answer: bool


class ConversationOutcome(ContractModel):
    scenario_id: Identifier
    suite: Identifier
    category: Identifier
    level: int
    judge_required: bool
    turns: int
    run_statuses: tuple[str, ...] = ()
    runner_status: str
    runner_reason: str | None = None
    error_type: str | None = None
    strict_checks_passed: int = 0
    strict_checks_total: int = 0
    figures: tuple[FigureCheck, ...] = ()
    safety_flags: Mapping[Identifier, bool] = {}
    provider: ProviderUse
    measurements: Mapping[Identifier, float] = {}
    transcript: str | None = None


class RealModelEvaluation(ContractModel):
    schema_version: Literal[1] = 1
    recorded_on: str
    code_version: str
    target_id: str
    execution_backend: str
    primary_provider: str
    configured_models: Mapping[str, str] = {}
    # manifest id -> manifest version; plus data references (extract digest).
    datasets: Mapping[str, str] = {}
    conversations: tuple[ConversationOutcome, ...]


# Conversational efficiency suite (T39-F3): what a request costs and whether
# its answer is right. Suite definitions are fixed before any run; results
# hold identifiers, codes, counts, expected figures and (separately written)
# released text of pseudonymized evaluation data only. Never thought text.

TurnKind = Literal["cold", "follow_up", "reuse", "clarification", "investigation"]


class EfficiencyTargets(ContractModel):
    """Evaluation targets of one turn (not production caps); None = no target."""

    max_queries: int | None = Field(default=None, ge=0)
    max_model_requests: int | None = Field(default=None, ge=1)
    # Successful queries allowed before a clarification question is asked.
    max_queries_before_question: int | None = Field(default=None, ge=0)


class ExpectedFigureRef(ContractModel):
    """An expected figure taken from the independent reference values.

    ``ref`` is ``<query>.<output>`` in the frozen extract's ``expected.json``;
    ``minus`` (optional) subtracts another reference (a derived difference).
    """

    name: Identifier
    ref: str
    minus: str | None = None
    kind: Literal["number", "label"] = "number"


class EfficiencyTurn(ContractModel):
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    kind: TurnKind
    targets: EfficiencyTargets = EfficiencyTargets()
    figures: tuple[ExpectedFigureRef, ...] = ()
    # Each group: at least one of its words must appear in the released text
    # (period, definition wording). Case-insensitive.
    text_terms: tuple[tuple[str, ...], ...] = ()
    # Each group: at least one of its fragments must appear in some SQL the
    # turn executed (whitespace- and case-insensitive).
    sql_terms: tuple[tuple[str, ...], ...] = ()
    expect_clarification: bool = False
    # Sent as the answer when the run asks a clarification question.
    clarification_reply: str | None = None
    expect_report: bool = False


class EfficiencyScenario(ContractModel):
    id: Identifier
    title: str
    category: Identifier
    # Executive scope name in the frozen extract's spec (``women``/``men``).
    scope: Identifier
    repeats: int = Field(ge=1, le=10)
    turns: tuple[EfficiencyTurn, ...] = Field(min_length=1)


class SpendCeiling(ContractModel):
    """Declared before the run; the runner refuses work beyond it."""

    max_total_tokens: int = Field(ge=1)
    max_model_attempts: int = Field(ge=1)
    # Worst case one run may add (the per-run budget limits).
    run_tokens_limit: int = Field(ge=1)
    run_requests_limit: int = Field(ge=1)


class EfficiencySuite(ContractModel):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    suite_version: Identifier
    declared_on: str
    spend: SpendCeiling
    scenarios: tuple[EfficiencyScenario, ...] = Field(min_length=1)


class ModelAttemptRecord(ContractModel):
    provider: str
    model: str
    outcome: str
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_from: str = "none"
    reason_class: str = "none"


class QueryRecord(ContractModel):
    """One ``execute_analysis`` attempt of a run: outcome and, when it
    produced evidence, the model's logical SQL (pseudonymized evaluation data
    only).

    ``outcome`` is ``succeeded`` (the warehouse query ran), ``rejected``
    (refused before reaching the warehouse: compiler or input validation;
    never counted as a warehouse query) or the failure/status otherwise.
    """

    outcome: str
    error_code: str | None = None
    evidence_id: str | None = None
    rows: int | None = None
    sql: str | None = None
    # Bound query parameters (name -> value as text).
    parameters: Mapping[str, str] = {}
    # A warehouse job was registered for the attempt (None: not recorded).
    executed: bool | None = None
    # Durable reason code (e.g. ``compile_unsupported_join``), if any.
    reason: str | None = None
    # What the tool call returned to the model (``succeeded``, ``failed`` ...);
    # can differ from ``outcome`` when a query ran but stored no evidence.
    tool_result: str | None = None
    # The SQL as the model wrote it (sanitized telemetry capture), recorded
    # when the attempt produced no evidence.
    attempted_sql: str | None = None
    attempted_parameters: Mapping[str, str] = {}


class TurnResult(ContractModel):
    turn: int
    kind: TurnKind
    run_id: str
    run_status: str
    tools: tuple[str, ...] = ()
    queries_succeeded: int = 0
    # Executed (or unconfirmed) queries that failed; rejected attempts apart.
    queries_failed: int = 0
    # Attempts refused before reaching the warehouse (compiler, input).
    queries_rejected: int = 0
    queries: tuple[QueryRecord, ...] = ()
    queries_before_question: int | None = None
    asked_clarification: bool = False
    attempts: tuple[ModelAttemptRecord, ...] = ()
    model_requests: int = 0
    model_requests_failed: int = 0
    fallbacks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    budget_tokens: int = 0
    active_seconds: float = 0.0
    wall_seconds: float = 0.0
    restarts: tuple[str, ...] = ()
    admission: str | None = None
    stop_reason: str | None = None
    figures: tuple[FigureCheck, ...] = ()
    text_terms_met: tuple[bool, ...] = ()
    sql_terms_met: tuple[bool, ...] = ()
    citations: int = 0
    cited_unknown: int = 0
    report_saved: bool | None = None
    report_actions: int | None = None
    max_evidence_rows: int = 0
    targets_met: Mapping[str, bool] = {}
    # True when the turn used more queries than its target.
    extra_queries: int = 0


class RepetitionResult(ContractModel):
    scenario_id: Identifier
    repetition: int = Field(ge=1)
    session_id: str
    executive_id: str
    started_at: str
    code_revision: str
    turns: tuple[TurnResult, ...] = ()
    error: str | None = None
    safety_flags: Mapping[Identifier, bool] = {}
    transcript: str | None = None


class EfficiencyRun(ContractModel):
    schema_version: Literal[1] = 1
    label: Identifier
    suite_id: Identifier
    suite_version: Identifier
    # Version of the target scoring applied (results may be rescored later).
    scoring_version: int = 1
    recorded_at: str
    code_revision: str
    target_id: str
    execution_backend: str
    warehouse: str
    data_ref: str
    extract_digest: str
    primary_provider: str
    configured_models: Mapping[str, str] = {}
    settings: Mapping[str, float | str] = {}
    spend: SpendCeiling
    spend_used: Mapping[str, int] = {}
    stopped_by_ceiling: bool = False
    repetitions: tuple[RepetitionResult, ...] = ()
