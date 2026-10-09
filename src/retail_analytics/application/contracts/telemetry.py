"""Telemetry vocabulary shared by the application and its sink adapters.

Span attributes carry correlation identifiers and bounded outcome codes;
metrics carry only the low-cardinality labels listed in :class:`Label`.
Interaction content (model messages, tool arguments and results, SQL, the
user's messages and released answers) travels separately as a
:class:`CapturedPayload`: already sanitized and bounded by
``application.telemetry_payloads``, and representation-neutral (a sink adapter
decides how its backend displays it).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

type AttributeValue = str | int | float | bool
type Attributes = dict[str, AttributeValue]


class Metric(StrEnum):
    """Metric names. Counters end in ``_total``; histograms carry a unit."""

    RUNS = "ra_runs_total"
    RUN_SECONDS = "ra_run_seconds"
    RUNS_STARTED = "ra_runs_started_total"
    HTTP_REQUESTS = "ra_http_requests_total"
    HTTP_SECONDS = "ra_http_request_seconds"
    TOOL_CALLS = "ra_tool_calls_total"
    TOOL_SECONDS = "ra_tool_seconds"
    TOOL_RETRIES = "ra_tool_retries_total"
    MODEL_REQUESTS = "ra_model_requests_total"
    MODEL_SECONDS = "ra_model_request_seconds"
    MODEL_TOKENS = "ra_model_tokens_total"
    MODEL_FALLBACKS = "ra_model_fallbacks_total"
    MODEL_ANSWERS = "ra_model_answers_total"
    FINAL_ANSWERS = "ra_final_answers_total"
    QUERIES = "ra_queries_total"
    QUERY_SECONDS = "ra_query_seconds"
    QUERY_BYTES = "ra_query_bytes_total"
    COMPILER_REJECTIONS = "ra_compiler_rejections_total"
    GATE_WITHHOLDS = "ra_output_gate_withholds_total"
    RETRIEVALS = "ra_retrievals_total"
    RETRIEVAL_SECONDS = "ra_retrieval_seconds"
    RETRIEVAL_SAMPLES = "ra_retrieval_review_samples_total"
    BUDGET_STOPS = "ra_budget_stops_total"
    # Model conversations restarted because their source context changed.
    CONTEXT_RESTARTS = "ra_context_restarts_total"
    RUN_BUDGET_USE = "ra_run_budget_use_ratio"
    TELEMETRY_DROPPED = "ra_telemetry_dropped_total"


HISTOGRAMS = frozenset(
    {
        Metric.RUN_SECONDS,
        Metric.HTTP_SECONDS,
        Metric.TOOL_SECONDS,
        Metric.MODEL_SECONDS,
        Metric.QUERY_SECONDS,
        Metric.RETRIEVAL_SECONDS,
        Metric.RUN_BUDGET_USE,
    }
)


class Label(StrEnum):
    """The only metric label keys. Identifiers are never labels."""

    STATUS = "status"
    OUTCOME = "outcome"
    CAPABILITY = "capability"
    ERROR_CODE = "error_code"
    REASON = "reason"
    PROVIDER = "provider"
    MODEL = "model"
    FROM_PROVIDER = "from_provider"
    TO_PROVIDER = "to_provider"
    REASON_CLASS = "reason_class"
    DIRECTION = "direction"
    ROUTE = "route"
    METHOD = "method"
    CAUSE_TYPE = "cause_type"
    RESOURCE = "resource"
    KIND = "kind"
    SERVICE = "service"


class ReasonClass(StrEnum):
    """Why a model request failed, coarse enough to chart."""

    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    REJECTED = "rejected"
    COOLING_DOWN = "cooling_down"
    BUDGET = "budget"
    OTHER = "other"


class Span(StrEnum):
    """Span names."""

    RUN = "investigation.run"
    ACCEPT = "run.accept"
    # Request admission: decision, topic, reason code, classifier version.
    ADMISSION = "run.admission"
    HTTP = "http.request"
    TOOL = "tool.call"
    MODEL_REQUEST = "model.request"
    MODEL_ATTEMPT = "model.attempt"
    QUERY = "query.execute"
    COMPILE = "query.compile"
    RETRIEVAL = "retrieval.search"
    ANSWER = "answer.release"
    LIFECYCLE = "run.step"
    CONTEXT_RESTART = "investigation.context_restart"
    USER_INPUT = "user.input"
    CLARIFICATION = "clarification.ask"


@dataclass(frozen=True, slots=True)
class ProviderAttribution:
    """Which provider actually answered one model request.

    Carried in the model response metadata so the final-answer step (a
    different activity, possibly another worker) can attribute the answer.
    Holds identifiers and codes only.
    """

    provider: str
    model: str
    attempt: int
    fallback_from: str | None = None
    fallback_reason: str | None = None


type PayloadValue = (
    bool | int | float | str | list[PayloadValue] | dict[str, PayloadValue] | None
)


class PayloadSide(StrEnum):
    """What a span received (inputs) or produced (outputs)."""

    INPUTS = "inputs"
    OUTPUTS = "outputs"


@dataclass(frozen=True, slots=True)
class CapturedPayload:
    """A sanitized, bounded interaction payload for one side of a span.

    ``content`` is plain JSON data with visible markers wherever something was
    masked (``[withheld]``/``[redacted]``), cut (``[truncated: ...]``) or left
    out (``[omitted: ...]``). ``chars`` is the size of the serialized content;
    ``omitted`` lists reason codes for the parts left out, so an empty
    ``content`` is never silent.
    """

    side: PayloadSide
    content: PayloadValue
    chars: int
    redactions: int = 0
    truncated: bool = False
    omitted: tuple[str, ...] = ()
