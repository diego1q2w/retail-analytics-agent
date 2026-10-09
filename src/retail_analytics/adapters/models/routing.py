"""Primary/backup provider routing with bounded retries.

The chain is Pydantic AI's ``FallbackModel`` over one ``ProviderAttempts``
per configured provider (Gemini primary, GPT backup)::

    ProviderRouting(FallbackModel(
        ProviderAttempts(BudgetedModel(StreamDeadlines(<gemini>))),
        ProviderAttempts(BudgetedModel(StreamDeadlines(<openai>))),
    ))

``ProviderAttempts`` owns retries for one provider. Transient failures
(throttling, server errors, connection failures, response timeouts) are
retried after the run budget's backoff decision, which honours the
provider's retry hint and the run's attempt and active-time limits. Every
attempt passes through ``BudgetedModel``, so retries and fallback attempts
reserve the same run budget before they are sent. A retry hint longer than
the configured maximum wait is not waited for: the request falls back and the
provider cools down. After a provider exhausts its attempts, or rejects the
key or model (401/403/404), it also cools down for a while, so later requests (any run
in this worker) go straight to the backup instead of probing an unavailable
provider again.

Fallback only repeats the *model request*. Tool calls are separate durable
activities whose effects are recorded once; the backup receives the same
application-owned history (tool results included) and never re-executes a
completed tool. Reasoning another provider produced is removed before a
request is sent, so no provider's private reasoning reaches another.

``ProviderRouting`` turns an exhausted chain into ``RunStopped`` (model
unavailable): the run stops with its verified findings instead of the
activity being retried with yet more provider attempts.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Protocol

from pydantic_ai.exceptions import FallbackExceptionGroup, ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ThinkingPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.profiles import DEFAULT_PROFILE, ModelProfile
from pydantic_ai.settings import ModelSettings

from retail_analytics.adapters.models.budgeted import current_run_id
from retail_analytics.adapters.models.capture import (
    error_payload,
    request_payload,
    response_payload,
)
from retail_analytics.adapters.models.deadlines import ModelResponseTimeout
from retail_analytics.application.budgets import RetryDecision
from retail_analytics.application.contracts.investigations import StopReason
from retail_analytics.application.contracts.telemetry import (
    Label,
    Metric,
    ProviderAttribution,
    ReasonClass,
    Span,
)
from retail_analytics.application.investigation_runtime import RunStopped
from retail_analytics.application.telemetry import (
    ATTRIBUTION_METADATA_KEY,
    Stopwatch,
    attribution_from_metadata,
    attribution_to_metadata,
    classify_reason_text,
    telemetry,
)

TRANSIENT_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
MISCONFIGURED = frozenset({401, 403, 404})


class RetryAdvisor(Protocol):
    """``RunBudgets.retry_decision``: attempts, backoff and active time."""

    async def retry_decision(
        self, run_id: str, failures: int, *, retry_after: float | None = None
    ) -> RetryDecision: ...


class ProviderCoolingDown(ModelAPIError):
    """The provider recently failed; skipped without sending a request."""


def is_transient(error: Exception) -> bool:
    if isinstance(error, ModelHTTPError):
        return error.status_code in TRANSIENT_STATUS
    # Connection failures and response timeouts carry no status.
    return isinstance(error, ModelAPIError)


def is_misconfigured(error: Exception) -> bool:
    """Key or model not accepted: every request would fail the same way."""
    return isinstance(error, ModelHTTPError) and error.status_code in MISCONFIGURED


def retry_after_seconds(error: Exception) -> float | None:
    headers = getattr(error, "headers", None)
    value = headers.get("retry-after") if isinstance(headers, dict) else None
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None


def reason_class(error: BaseException) -> ReasonClass:
    """Coarse class of a failed model request (never the error text)."""
    if isinstance(error, ProviderCoolingDown):
        return ReasonClass.COOLING_DOWN
    if isinstance(error, ModelResponseTimeout):
        return ReasonClass.TIMEOUT
    if isinstance(error, RunStopped):
        return ReasonClass.BUDGET
    if isinstance(error, ModelHTTPError):
        return classify_reason_text(error.status_code)
    if isinstance(error, ModelAPIError):
        return ReasonClass.CONNECTION
    return ReasonClass.OTHER


@dataclass
class RequestTrace:
    """What happened to one logical model request across the provider chain.

    ``failed`` lists each provider that did not answer, with the failure class,
    in order; the provider that finally answers reports a fallback from the
    last one. Shared through a context variable because the chain members are
    separate wrapper models.
    """

    attempts: int = 0
    failed: list[tuple[str, ReasonClass]] = field(default_factory=list)


_request_trace: ContextVar[RequestTrace | None] = ContextVar(
    "model_request_trace", default=None
)


def _safe_run_id(run_id: Callable[[], str]) -> str | None:
    try:
        return run_id()
    except RunStopped:
        return None


def _served_attributes(served: ProviderAttribution) -> dict[str, object]:
    return {
        "answered_by": served.provider,
        "answered_model": served.model,
        "fallback_from": served.fallback_from or "none",
        "fallback_reason": served.fallback_reason or "none",
    }


def _usage_counts(response: ModelResponse) -> tuple[int, int]:
    usage = response.usage
    return max(usage.input_tokens, 0), max(usage.output_tokens, 0)


def without_foreign_reasoning(
    messages: list[ModelMessage], provider: str
) -> list[ModelMessage]:
    cleaned: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelResponse) and any(
            isinstance(part, ThinkingPart) and part.provider_name != provider
            for part in message.parts
        ):
            message = replace(
                message,
                parts=[
                    part
                    for part in message.parts
                    if not (
                        isinstance(part, ThinkingPart)
                        and part.provider_name != provider
                    )
                ],
            )
        cleaned.append(message)
    return cleaned


class ProviderAttempts(WrapperModel):
    def __init__(
        self,
        wrapped: Model,
        retries: RetryAdvisor,
        *,
        provider: str,
        max_wait_seconds: float,
        cooldown_seconds: float = 0.0,
        run_id: Callable[[], str] = current_run_id,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(wrapped)
        self._retries = retries
        self._reasoning_owner = provider
        self._max_wait = max_wait_seconds
        self._cooldown = cooldown_seconds
        self._run_id = run_id
        self._clock = clock
        self._sleep = sleep
        self._cooling_until = 0.0

    @property
    def cooling_down(self) -> bool:
        return self._clock() < self._cooling_until

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        trace = _request_trace.get()
        if self.cooling_down:
            error = ProviderCoolingDown(self.model_name, "provider cooling down")
            if trace is not None:
                trace.failed.append((self._reasoning_owner, ReasonClass.COOLING_DOWN))
            telemetry().count(
                Metric.MODEL_REQUESTS,
                {
                    Label.PROVIDER: self._reasoning_owner,
                    Label.MODEL: self.model_name,
                    Label.OUTCOME: "skipped",
                    Label.REASON_CLASS: ReasonClass.COOLING_DOWN.value,
                },
            )
            raise error
        messages = without_foreign_reasoning(messages, self._reasoning_owner)
        failures = 0
        while True:
            try:
                return await self._attempt(
                    messages,
                    model_settings,
                    model_request_parameters,
                    failures + 1,
                    trace,
                )
            except ModelAPIError as error:
                if not is_transient(error):
                    if is_misconfigured(error):
                        self._cool_down(0.0)
                    self._failed(trace, error)
                    raise
                failures += 1
                hint = retry_after_seconds(error)
                if hint is not None and hint > self._max_wait:
                    self._cool_down(hint)
                    self._failed(trace, error)
                    raise
                decision = await self._retries.retry_decision(
                    self._run_id(), failures, retry_after=hint
                )
                if not decision.allowed:
                    self._cool_down(hint or 0.0)
                    self._failed(trace, error)
                    raise
                await self._sleep(decision.delay_seconds)

    def _failed(self, trace: RequestTrace | None, error: BaseException) -> None:
        if trace is not None:
            trace.failed.append((self._reasoning_owner, reason_class(error)))

    async def _attempt(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        attempt: int,
        trace: RequestTrace | None,
    ) -> ModelResponse:
        """One request to this provider, observed: provider, model, attempt
        number, outcome and (when a previous provider failed) the fallback."""
        provider, model = self._reasoning_owner, self.model_name
        if trace is not None:
            trace.attempts += 1
        origin = trace.failed[-1] if trace is not None and trace.failed else None
        watch = Stopwatch()
        labels = {Label.PROVIDER: provider, Label.MODEL: model}
        with telemetry().span(
            Span.MODEL_ATTEMPT,
            run_id=_safe_run_id(self._run_id),
            attributes={
                "provider": provider,
                "model": model,
                "attempt": attempt,
                "request_sequence": trace.attempts if trace is not None else 1,
                "fallback_from": origin[0] if origin else "none",
                "fallback_reason": origin[1].value if origin else "none",
            },
        ) as span:
            if span.captures:
                # What this provider is sent: after foreign reasoning removal,
                # with the guarded context in front (GuardedModel).
                span.inputs(
                    request_payload(
                        messages,
                        model_request_parameters,
                        provider=provider,
                        model=model,
                        attempt=attempt,
                    )
                )
            try:
                response = await self.wrapped.request(
                    messages, model_settings, model_request_parameters
                )
            except Exception as error:
                reason = reason_class(error)
                span.set({"outcome": "failed", "reason_class": reason.value})
                if span.captures:
                    span.outputs(error_payload(error, reason.value))
                telemetry().count(
                    Metric.MODEL_REQUESTS,
                    {
                        **labels,
                        Label.OUTCOME: "failed",
                        Label.REASON_CLASS: reason.value,
                    },
                )
                telemetry().observe(Metric.MODEL_SECONDS, watch.seconds(), labels)
                raise
            tokens_in, tokens_out = _usage_counts(response)
            if span.captures:
                span.outputs(response_payload(response))
            span.set(
                {
                    "outcome": "succeeded",
                    "reason_class": "none",
                    "input_tokens": tokens_in,
                    "output_tokens": tokens_out,
                }
            )
        telemetry().count(
            Metric.MODEL_REQUESTS,
            {**labels, Label.OUTCOME: "succeeded", Label.REASON_CLASS: "none"},
        )
        telemetry().observe(Metric.MODEL_SECONDS, watch.seconds(), labels)
        telemetry().count(
            Metric.MODEL_TOKENS, {**labels, Label.DIRECTION: "input"}, tokens_in
        )
        telemetry().count(
            Metric.MODEL_TOKENS, {**labels, Label.DIRECTION: "output"}, tokens_out
        )
        if origin is not None:
            telemetry().count(
                Metric.MODEL_FALLBACKS,
                {
                    Label.FROM_PROVIDER: origin[0],
                    Label.TO_PROVIDER: provider,
                    Label.REASON_CLASS: origin[1].value,
                },
            )
        attribution = ProviderAttribution(
            provider,
            model,
            attempt,
            origin[0] if origin else None,
            origin[1].value if origin else None,
        )
        metadata = {
            **(response.metadata or {}),
            ATTRIBUTION_METADATA_KEY: attribution_to_metadata(attribution),
        }
        return replace(response, metadata=metadata)

    def _cool_down(self, hint: float) -> None:
        if self._cooldown > 0:
            self._cooling_until = self._clock() + max(self._cooldown, hint)


class ProviderRouting(WrapperModel):
    """The configured chain as the investigation's single model."""

    def __init__(
        self,
        members: Sequence[Model],
        *,
        run_id: Callable[[], str] = current_run_id,
    ) -> None:
        if not members:
            raise ValueError("at least one provider is required")
        super().__init__(FallbackModel(*members))
        self._run_id = run_id

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        trace = RequestTrace()
        token = _request_trace.set(trace)
        with telemetry().span(
            Span.MODEL_REQUEST, run_id=_safe_run_id(self._run_id)
        ) as span:
            try:
                response = await self.wrapped.request(
                    messages, model_settings, model_request_parameters
                )
            except (ModelAPIError, FallbackExceptionGroup) as error:
                span.set(
                    {
                        "outcome": "exhausted",
                        "attempts": trace.attempts,
                        "providers_failed": len(trace.failed),
                    }
                )
                # Retries and fallback are spent; never multiply them through
                # activity retries. The run ends with its verified findings.
                raise RunStopped(StopReason.MODEL_UNAVAILABLE) from error
            finally:
                _request_trace.reset(token)
            served = attribution_from_metadata(
                (response.metadata or {}).get(ATTRIBUTION_METADATA_KEY)
            )
            span.set(
                {
                    "outcome": "succeeded",
                    "attempts": trace.attempts,
                    "fallback": bool(trace.failed),
                    **({} if served is None else _served_attributes(served)),
                }
            )
        return response

    @property
    def profile(self) -> ModelProfile:
        # Each provider shapes tool schemas with its own profile; the chain
        # itself must not pre-transform them for one provider.
        return DEFAULT_PROFILE

    @property
    def model_name(self) -> str:
        return self.wrapped.model_name

    def customize_request_parameters(
        self, model_request_parameters: ModelRequestParameters
    ) -> ModelRequestParameters:
        return model_request_parameters

    def prepare_request(
        self,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ModelSettings | None, ModelRequestParameters]:
        return model_settings, model_request_parameters

    def prepare_messages(
        self,
        messages: list[ModelMessage],
        model_request_parameters: ModelRequestParameters | None = None,
    ) -> list[ModelMessage]:
        return messages
