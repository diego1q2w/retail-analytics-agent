"""Run-budget accounting around every model request actually sent.

``BudgetedModel`` wraps one provider model (for example each member of a
primary/backup ``FallbackModel``), so every request that reaches a provider -
including fallback attempts and Temporal activity retries - is reserved
against the run's shared budget *before* it is sent and settled with the
reported token usage afterwards. A reservation refused by the budget stops
the run (``RunStopped``) instead of sending the request.

The request key is stable for one attempt of one model activity and differs
for every new attempt, so a retried activity (which may have sent its request
before the worker died) is charged again: provider attempts are never
undercounted.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai._run_context import get_current_run_context
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import (
    Model,
    ModelRequestParameters,
    StreamedResponse,
)
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from pydantic_core import to_json
from temporalio import activity

from retail_analytics.application.budgets import ProviderBudget, ProviderUsage
from retail_analytics.application.investigation_runtime import RunStopped, StopReason
from retail_analytics.domain.budgets import BudgetExhausted

CHARS_PER_TOKEN = 4


def current_run_id() -> str:
    """The investigation run of the agent run in progress (its ``deps.run_id``)."""
    context = get_current_run_context()
    run_id = getattr(getattr(context, "deps", None), "run_id", None)
    if not isinstance(run_id, str) or not run_id:
        raise RunStopped(StopReason.INTERRUPTED)
    return run_id


def activity_request_key(model_name: str) -> str:
    """Stable per activity attempt; a fresh key outside an activity."""
    if activity.in_activity():
        info = activity.info()
        return (f"{info.workflow_id}/{info.activity_id}/{info.attempt}/{model_name}")[
            :200
        ]
    return f"local/{uuid.uuid4().hex}/{model_name}"[:200]


def estimate_input_tokens(messages: list[ModelMessage]) -> int:
    return max(len(to_json(messages)) // CHARS_PER_TOKEN, 1)


class BudgetedModel(WrapperModel):
    def __init__(
        self,
        wrapped: Model,
        budget: ProviderBudget,
        *,
        run_id: Callable[[], str] = current_run_id,
        request_key: Callable[[str], str] = activity_request_key,
    ) -> None:
        super().__init__(wrapped)
        self._budget = budget
        self._run_id = run_id
        self._request_key = request_key

    async def _reserve(self, messages: list[ModelMessage]) -> tuple[str, str]:
        run_id = self._run_id()
        key = self._request_key(self.model_name)
        try:
            await self._budget.reserve_provider_request(
                run_id, key, estimated_input_tokens=estimate_input_tokens(messages)
            )
        except BudgetExhausted as exhausted:
            raise RunStopped(StopReason.BUDGET, exhausted.resource) from None
        return run_id, key

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        run_id, key = await self._reserve(messages)
        try:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        except BaseException:
            # Sent (or maybe sent) without reported usage: the estimate stands.
            await self._budget.record_provider_usage(run_id, key, ProviderUsage())
            raise
        await self._budget.record_provider_usage(
            run_id,
            key,
            ProviderUsage(response.usage.input_tokens, response.usage.output_tokens),
        )
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        run_id, key = await self._reserve(messages)
        usage = ProviderUsage()
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                yield stream
                usage = ProviderUsage(
                    stream.usage.input_tokens, stream.usage.output_tokens
                )
        finally:
            await self._budget.record_provider_usage(run_id, key, usage)
