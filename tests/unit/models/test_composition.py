"""Model settings and the live provider chain composition (no network)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIResponsesModel

from retail_analytics.adapters.models.gemini_interactions import (
    GeminiInteractionsModel,
)
from retail_analytics.adapters.models.routing import ProviderAttempts, ProviderRouting
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    load_backend_settings,
)
from retail_analytics.bootstrap.models import provider_chain, response_limits
from retail_analytics.domain.budgets import RunLimits
from tests.unit.budgets.memory_store import MemoryRunBudgetStore

KEY = SecretStr("k" * 40)


def _members(settings: BackendSettings) -> list[ProviderAttempts]:
    chain = provider_chain(settings)(RunBudgets(MemoryRunBudgetStore(), RunLimits()))
    assert isinstance(chain, ProviderRouting)
    assert isinstance(chain.wrapped, FallbackModel)
    members = chain.wrapped.models
    assert all(isinstance(m, ProviderAttempts) for m in members)
    return members  # type: ignore[return-value]


def _raw(member: ProviderAttempts) -> object:
    return member.wrapped.wrapped.wrapped  # type: ignore[attr-defined]


def test_defaults_select_gemini_primary_and_gpt_backup() -> None:
    settings = BackendSettings(gemini_api_key=KEY, openai_api_key=KEY)
    assert settings.agent_gemini_model == "gemini-3.8-flash"
    assert settings.agent_openai_model == "gpt-5-mini"
    limits = response_limits(settings)
    assert (limits.first_token_seconds, limits.stall_seconds) == (60, 30)
    assert limits.total_seconds == 180
    primary, backup = _members(settings)
    assert isinstance(_raw(primary), GeminiInteractionsModel)
    assert _raw(primary).model_name == "gemini-3.8-flash"  # type: ignore[attr-defined]
    assert isinstance(_raw(backup), OpenAIResponsesModel)
    assert _raw(backup).model_name == "gpt-5-mini"  # type: ignore[attr-defined]


def test_without_an_openai_key_there_is_no_backup() -> None:
    (only,) = _members(BackendSettings(gemini_api_key=KEY))
    assert isinstance(_raw(only), GeminiInteractionsModel)


def test_the_primary_is_required() -> None:
    with pytest.raises(ConfigError, match="RETAIL_ANALYTICS_GEMINI_API_KEY"):
        provider_chain(BackendSettings(openai_api_key=KEY))


def test_model_settings_load_and_validate() -> None:
    settings = load_backend_settings(
        environ={
            "RETAIL_ANALYTICS_AGENT_GEMINI_MODEL": "gemini-3-flash-preview",
            "RETAIL_ANALYTICS_MODEL_FIRST_TOKEN_SECONDS": "90",
            "RETAIL_ANALYTICS_MODEL_STREAM_STALL_SECONDS": "45",
        },
        env_file=None,
    )
    assert settings.agent_gemini_model == "gemini-3-flash-preview"
    assert response_limits(settings).first_token_seconds == 90
    with pytest.raises(ConfigError, match="MODEL_FIRST_TOKEN_SECONDS"):
        load_backend_settings(
            environ={
                "RETAIL_ANALYTICS_MODEL_FIRST_TOKEN_SECONDS": "120",
                "RETAIL_ANALYTICS_MODEL_REQUEST_MAX_SECONDS": "60",
            },
            env_file=None,
        )


def test_chain_repr_does_not_expose_keys() -> None:
    secret = "sk-very-secret-provider-key-123456"
    settings = BackendSettings(
        gemini_api_key=SecretStr(secret), openai_api_key=SecretStr(secret)
    )
    chain = provider_chain(settings)(RunBudgets(MemoryRunBudgetStore(), RunLimits()))
    assert secret not in repr(chain)
    assert secret not in str(settings.redacted_summary())
