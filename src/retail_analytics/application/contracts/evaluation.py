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
