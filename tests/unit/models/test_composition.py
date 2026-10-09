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
    RuntimeMode,
    load_backend_settings,
)
from retail_analytics.bootstrap.models import (
    provider_chain,
    provider_summary,
    response_limits,
)
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
    settings = BackendSettings(
        mode=RuntimeMode.FIXTURE, gemini_api_key=KEY, openai_api_key=KEY
    )
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
    (only,) = _members(BackendSettings(mode=RuntimeMode.FIXTURE, gemini_api_key=KEY))
    assert isinstance(_raw(only), GeminiInteractionsModel)


def test_the_primary_is_required() -> None:
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        provider_chain(BackendSettings(mode=RuntimeMode.FIXTURE, openai_api_key=KEY))


def test_model_settings_load_and_validate() -> None:
    settings = load_backend_settings(
        environ={
            "APP_MODE": "fixture",
            "AGENT_GEMINI_MODEL": "gemini-3-flash-preview",
            "MODEL_FIRST_TOKEN_SECONDS": "90",
            "MODEL_STREAM_STALL_SECONDS": "45",
        },
        env_file=None,
    )
    assert settings.agent_gemini_model == "gemini-3-flash-preview"
    assert response_limits(settings).first_token_seconds == 90
    with pytest.raises(ConfigError, match="MODEL_FIRST_TOKEN_SECONDS"):
        load_backend_settings(
            environ={
                "APP_MODE": "fixture",
                "MODEL_FIRST_TOKEN_SECONDS": "120",
                "MODEL_REQUEST_MAX_SECONDS": "60",
            },
            env_file=None,
        )


def test_chain_repr_does_not_expose_keys() -> None:
    secret = "sk-very-secret-provider-key-123456"
    settings = BackendSettings(
        mode=RuntimeMode.FIXTURE,
        gemini_api_key=SecretStr(secret),
        openai_api_key=SecretStr(secret),
    )
    chain = provider_chain(settings)(RunBudgets(MemoryRunBudgetStore(), RunLimits()))
    assert secret not in repr(chain)
    assert secret not in str(settings.redacted_summary())


def test_provider_summary_names_providers_never_keys() -> None:
    key = "test-openai-key-must-not-be-printed"
    fixture = BackendSettings(mode=RuntimeMode.FIXTURE)
    assert "fixture" in provider_summary(fixture)
    live = BackendSettings(
        mode=RuntimeMode.LIVE,
        database_url=SecretStr("postgresql://u:p@h/d"),
        bigquery_project="p",
        gemini_api_key=SecretStr("g" * 40),
        auth_signing_key=SecretStr("s" * 40),
    )
    assert provider_summary(live) == (
        "model providers: gemini (primary), no fallback (OPENAI_API_KEY unset)"
    )
    both = live.model_copy(update={"openai_api_key": SecretStr(key)})
    line = provider_summary(both)
    assert line == (
        "model providers: gemini (primary), openai (fallback, OPENAI_API_KEY set)"
    )
    assert key not in line
