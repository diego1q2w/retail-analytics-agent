"""Run-budget accounting around every model request actually sent.

``BudgetedModel`` wraps one provider model (for example each member of a
primary/backup ``FallbackModel``), so every request that reaches a provider -
including fallback attempts and runtime retries - is reserved
against the run's shared budget *before* it is sent and settled with the
reported token usage afterwards. A reservation refused by the budget stops
the run (``RunStopped``) instead of sending the request.

The request key combines the execution attempt (the ``request_scope`` a
runtime sets: the Temporal adapter uses the model activity attempt) with a
per-request sequence number, so a retried attempt (which may have sent its
request before the worker died), an in-attempt retry and a fallback attempt
are each charged again: provider attempts are never undercounted.

Each settled request also carries its estimated dollar cost (input, cached
input and output priced per the provider's API); the budget refuses the next
request once the run's estimated spend reaches its limit. The settled charge
is handed to the enclosing ``ProviderAttempts`` (``attempt_costs``) for the
attempt's trace attributes and metrics; enforcement never depends on them.

Settlement records actual usage only when the provider reported it. A
response without usage, a lost response or a timeout keeps the reservation
estimate (the outcome is ambiguous); a definite client-side rejection
(HTTP 4xx, for example 429) settles at zero tokens but still counts as a
request.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic_ai._run_context import get_current_run_context
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import (
    Model,
    ModelRequestParameters,
    StreamedResponse,
)
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RequestUsage
from pydantic_core import to_json

from retail_analytics.application.contracts.budgets import ProviderUsage
from retail_analytics.application.contracts.investigations import StopReason
from retail_analytics.application.contracts.model_costs import ModelRef
from retail_analytics.application.investigation_runtime import RunStopped
from retail_analytics.application.ports.budgets import ProviderBudget
from retail_analytics.domain.budgets import BudgetExhausted, Charge

CHARS_PER_TOKEN = 4


def current_run_id() -> str:
    """The investigation run of the agent run in progress (its ``deps.run_id``)."""
    context = get_current_run_context()
    run_id = getattr(getattr(context, "deps", None), "run_id", None)
    if not isinstance(run_id, str) or not run_id:
        raise RunStopped(StopReason.INTERRUPTED)
    return run_id


_request_scope: ContextVar[str | None] = ContextVar(
    "retail_analytics_request_scope", default=None
)


@contextmanager
def request_scope(scope: str | None) -> Iterator[None]:
    """Name the execution attempt whose provider requests follow.

    A runtime that retries a model step (for example a Temporal activity
    attempt) sets a scope that is stable for that attempt; None leaves every
    request unscoped (a fresh key each).
    """
    token = _request_scope.set(scope)
    try:
        yield
    finally:
        _request_scope.reset(token)


def scoped_request_key(model_name: str) -> str:
    """Stable per execution attempt; a fresh key outside any scope."""
    scope = _request_scope.get()
    if scope is not None:
        return f"{scope}/{model_name}"[:200]
    return f"local/{uuid.uuid4().hex}/{model_name}"[:200]


_attempt_costs: ContextVar[list[Charge] | None] = ContextVar(
    "retail_analytics_attempt_costs", default=None
)


@contextmanager
def attempt_costs() -> Iterator[list[Charge]]:
    """Collect the settled charges of the requests sent inside the block."""
    sink: list[Charge] = []
    token = _attempt_costs.set(sink)
    try:
        yield sink
    finally:
        _attempt_costs.reset(token)


def reported_usage(usage: RequestUsage) -> ProviderUsage:
    """What the provider reported; unknown when it reported nothing.

    Every real request has input tokens, so zero input means the usage was
    not reported (for example a stream cut off before its final event).
    Pydantic AI's request usage is already normalized: input includes the
    cached part and output includes reasoning; the subsets come along for
    pricing and audit.
    """
    if usage.input_tokens <= 0:
        return ProviderUsage()
    reasoning = (usage.details or {}).get("reasoning_tokens")
    return ProviderUsage(
        usage.input_tokens,
        usage.output_tokens,
        cached_input_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=reasoning if isinstance(reasoning, int) else None,
    )


def failure_usage(error: BaseException) -> ProviderUsage:
    if isinstance(error, ModelHTTPError) and 400 <= error.status_code < 500:
        return ProviderUsage(0, 0)
    return ProviderUsage()


def estimate_input_tokens(messages: list[ModelMessage]) -> int:
    return max(len(to_json(messages)) // CHARS_PER_TOKEN, 1)


class BudgetedModel(WrapperModel):
    def __init__(
        self,
        wrapped: Model,
        budget: ProviderBudget,
        *,
        run_id: Callable[[], str] = current_run_id,
        request_key: Callable[[str], str] = scoped_request_key,
    ) -> None:
        super().__init__(wrapped)
        self._budget = budget
        self._run_id = run_id
        self._request_key = request_key
        self._sequence = itertools.count()

    @property
    def model_ref(self) -> ModelRef:
        return ModelRef(self.system, self.model_name)

    async def _reserve(self, messages: list[ModelMessage]) -> tuple[str, str, int]:
        run_id = self._run_id()
        key = f"{self._request_key(self.model_name)}/{next(self._sequence)}"
        estimate = estimate_input_tokens(messages)
        try:
            await self._budget.reserve_provider_request(
                run_id, key, estimated_input_tokens=estimate, model=self.model_ref
            )
        except BudgetExhausted as exhausted:
            raise RunStopped(StopReason.BUDGET, exhausted.resource) from None
        return run_id, key, estimate

    async def _record(
        self, run_id: str, key: str, usage: ProviderUsage, estimate: int
    ) -> None:
        charge = await self._budget.record_provider_usage(
            run_id,
            key,
            usage,
            model=self.model_ref,
            estimated_input_tokens=estimate,
        )
        sink = _attempt_costs.get()
        if sink is not None and charge is not None:
            sink.append(charge)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        run_id, key, estimate = await self._reserve(messages)
        try:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        except BaseException as error:
            # Sent (or maybe sent) without reported usage: the estimate stands
            # unless the provider definitely rejected the request.
            await self._record(run_id, key, failure_usage(error), estimate)
            raise
        await self._record(run_id, key, reported_usage(response.usage), estimate)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        run_id, key, estimate = await self._reserve(messages)
        usage = ProviderUsage()
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
                usage = reported_usage(stream.usage)
        finally:
            await self._record(run_id, key, usage, estimate)
