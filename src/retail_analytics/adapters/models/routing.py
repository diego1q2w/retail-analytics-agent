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
from dataclasses import replace
from typing import Protocol

from pydantic_ai.exceptions import FallbackExceptionGroup, ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ThinkingPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.profiles import DEFAULT_PROFILE, ModelProfile
from pydantic_ai.settings import ModelSettings

from retail_analytics.adapters.models.budgeted import current_run_id
from retail_analytics.application.budgets import RetryDecision
from retail_analytics.application.investigation_runtime import RunStopped, StopReason

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
        if self.cooling_down:
            raise ProviderCoolingDown(self.model_name, "provider cooling down")
        messages = without_foreign_reasoning(messages, self._reasoning_owner)
        failures = 0
        while True:
            try:
                return await self.wrapped.request(
                    messages, model_settings, model_request_parameters
                )
            except ModelAPIError as error:
                if not is_transient(error):
                    if is_misconfigured(error):
                        self._cool_down(0.0)
                    raise
                failures += 1
                hint = retry_after_seconds(error)
                if hint is not None and hint > self._max_wait:
                    self._cool_down(hint)
                    raise
                decision = await self._retries.retry_decision(
                    self._run_id(), failures, retry_after=hint
                )
                if not decision.allowed:
                    self._cool_down(hint or 0.0)
                    raise
                await self._sleep(decision.delay_seconds)

    def _cool_down(self, hint: float) -> None:
        if self._cooldown > 0:
            self._cooling_until = self._clock() + max(self._cooldown, hint)


class ProviderRouting(WrapperModel):
    """The configured chain as the investigation's single model."""

    def __init__(self, members: Sequence[Model]) -> None:
        if not members:
            raise ValueError("at least one provider is required")
        super().__init__(FallbackModel(*members))

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        try:
            return await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        except (ModelAPIError, FallbackExceptionGroup) as error:
            # Retries and fallback are spent; never multiply them through
            # activity retries. The run ends with its verified findings.
            raise RunStopped(StopReason.MODEL_UNAVAILABLE) from error

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
