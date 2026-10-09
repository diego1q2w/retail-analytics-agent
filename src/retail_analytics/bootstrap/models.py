"""Composition of the investigation agent's provider chain (live mode).

Gemini (Interactions API) is the primary, the configured OpenAI model
(Responses API) the backup when its key is set. Each provider gets response
deadlines, run-budget accounting per actual request and bounded retries; see
``adapters.models.routing``. SDK-level retries are disabled so that every
attempt is one the application counted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from retail_analytics.adapters.models.budgeted import BudgetedModel
from retail_analytics.adapters.models.deadlines import ResponseLimits, StreamDeadlines
from retail_analytics.adapters.models.gemini_interactions import (
    GeminiInteractionsModel,
)
from retail_analytics.adapters.models.routing import ProviderAttempts, ProviderRouting
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    RuntimeMode,
    backend_env_name,
)

type ModelFactory = Callable[[RunBudgets], Model]


def provider_summary(settings: BackendSettings) -> str:
    """One startup line naming the enabled model providers (never values).

    The keys use the providers' own variable names, so a key exported in the
    shell (for example ``OPENAI_API_KEY``) can switch the fallback on; this
    line makes that visible.
    """
    if settings.mode is not RuntimeMode.LIVE:
        return "model providers: none (fixture mode: offline scripted model)"
    openai = backend_env_name("openai_api_key")
    fallback = (
        f"openai (fallback, {openai} set)"
        if settings.openai_api_key is not None
        else f"no fallback ({openai} unset)"
    )
    return f"model providers: gemini (primary), {fallback}"


def response_limits(settings: BackendSettings) -> ResponseLimits:
    return ResponseLimits(
        first_token_seconds=settings.model_first_token_seconds,
        stall_seconds=settings.model_stream_stall_seconds,
        total_seconds=settings.model_request_max_seconds,
    )


def _http_client(settings: BackendSettings) -> httpx.AsyncClient:
    # The stream deadlines bound the request; this is only a transport backstop.
    total = settings.model_request_max_seconds + 30
    return httpx.AsyncClient(timeout=httpx.Timeout(total, connect=15.0))


def gemini_model(
    settings: BackendSettings, *, http_client: httpx.AsyncClient | None = None
) -> GeminiInteractionsModel:
    if settings.gemini_api_key is None:
        raise ConfigError(["GEMINI_API_KEY: required for the agent"])
    return GeminiInteractionsModel(
        settings.agent_gemini_model,
        api_key=settings.gemini_api_key.get_secret_value(),
        http_client=http_client or _http_client(settings),
        max_output_tokens=settings.model_max_output_tokens,
    )


def openai_model(
    settings: BackendSettings, *, http_client: DefaultAsyncHttpxClient | None = None
) -> OpenAIResponsesModel | None:
    if settings.openai_api_key is None:
        return None
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        max_retries=0,
        timeout=settings.model_request_max_seconds + 30.0,
        http_client=http_client,
    )
    return OpenAIResponsesModel(
        settings.agent_openai_model,
        provider=OpenAIProvider(openai_client=client),
        settings=OpenAIResponsesModelSettings(
            # Nothing kept on the provider; history is sent by the application.
            openai_store=False,
            max_tokens=settings.model_max_output_tokens,
        ),
    )


def provider_chain(
    settings: BackendSettings, *, providers: Sequence[Model] | None = None
) -> ModelFactory:
    """The live chain, bound to the run budgets by the investigation root.

    ``providers`` replaces the real provider models, primary first (tests).
    """
    if providers is None:
        backup = openai_model(settings)
        providers = [gemini_model(settings), *([backup] if backup else [])]
    raws = list(providers)
    limits = response_limits(settings)
    # Skipping a cooling primary only makes sense when a backup exists.
    cooldown = settings.model_primary_cooldown_seconds if len(raws) > 1 else 0

    def build(budgets: RunBudgets) -> Model:
        return ProviderRouting(
            [
                ProviderAttempts(
                    BudgetedModel(StreamDeadlines(raw, limits), budgets),
                    budgets,
                    provider=raw.system,
                    max_wait_seconds=settings.retry_max_seconds,
                    cooldown_seconds=cooldown if index == 0 else 0,
                )
                for index, raw in enumerate(raws)
            ]
        )

    return build
