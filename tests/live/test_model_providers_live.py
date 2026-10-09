"""Live smoke of the agent's provider chain: few requests (free-tier limits).

Each test runs a small Pydantic AI agent with one function tool and a
structured answer through the real chain (deadlines, budget accounting,
retries, fallback). Skipped without the provider keys. Reads the ignored
``.env`` of the repository, or of the directory pytest started in (worktrees).
Prompts are synthetic; nothing from the dataset is sent.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from retail_analytics.application.budgets import RunBudgets
from retail_analytics.bootstrap.config import BackendSettings, load_backend_settings
from retail_analytics.bootstrap.models import gemini_model, openai_model, provider_chain
from retail_analytics.domain.budgets import ChargeKind, RunLimits
from tests.unit.budgets.memory_store import MemoryRunBudgetStore

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = next(
    (p for p in (ROOT / ".env", Path.cwd() / ".env") if p.is_file()), ROOT / ".env"
)

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    total: float


@dataclass(frozen=True)
class Deps:
    run_id: str


def _settings() -> BackendSettings:
    settings = load_backend_settings(environ={}, env_file=ENV_FILE)
    if settings.gemini_api_key is None or settings.openai_api_key is None:
        pytest.skip("RETAIL_ANALYTICS_GEMINI_API_KEY/OPENAI_API_KEY not set")
    return settings


class FailAfter(WrapperModel):
    """Serves ``ok`` requests, then answers 503 like an overloaded provider."""

    def __init__(self, wrapped: Model, ok: int) -> None:
        super().__init__(wrapped)
        self.ok = ok

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        raise AssertionError("the chain streams every request")

    def request_stream(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.ok <= 0:
            raise ModelHTTPError(503, self.model_name, {"code": "unavailable"})
        self.ok -= 1
        return self.wrapped.request_stream(*args, **kwargs)


class QuotaWatch(WrapperModel):
    """Notes a 429 from the provider (the free tier allows few requests)."""

    throttled = False

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        async with AsyncExitStack() as stack:
            try:
                stream = await stack.enter_async_context(
                    self.wrapped.request_stream(*args, **kwargs)
                )
            except ModelHTTPError as error:
                if error.status_code == 429:
                    QuotaWatch.throttled = True
                raise
            yield stream


def _gemini(settings: BackendSettings) -> Model:
    return QuotaWatch(gemini_model(settings))


async def _run(providers: list[Model], settings: BackendSettings) -> tuple:  # type: ignore[type-arg]
    store = MemoryRunBudgetStore()
    budgets = RunBudgets(store, RunLimits())
    chain = provider_chain(settings, providers=providers)(budgets)
    calls: list[str] = []
    agent: Agent[Deps, Answer] = Agent(
        chain,
        deps_type=Deps,
        output_type=Answer,
        instructions="Answer with figures from tools only.",
    )

    @agent.tool
    def monthly_sales(ctx: RunContext[Deps], month: str) -> dict[str, float]:
        """Total completed sales for a month (YYYY-MM)."""
        calls.append(month)
        return {"total": 1234.5}

    QuotaWatch.throttled = False
    try:
        result = await agent.run(
            "What were total sales in 2026-09? Use the tool.", deps=Deps("live-run")
        )
    finally:
        if QuotaWatch.throttled:
            # The scenario's premise (Gemini answering) did not hold.
            pytest.skip("Gemini quota exhausted (429); not a pass")
    charges = [
        c
        for c in await store.charges("live-run")
        if c.kind is ChargeKind.PROVIDER_REQUEST
    ]
    return result, calls, charges


def _assert_answer(result, calls, charges) -> None:  # type: ignore[no-untyped-def]
    assert calls == ["2026-09"]
    assert result.output.total == pytest.approx(1234.5)
    assert len(charges) >= 2
    assert all(c.settled for c in charges)


async def test_gemini_primary_answers_with_a_tool_and_reports_usage() -> None:
    settings = _settings()
    result, calls, charges = await _run([_gemini(settings)], settings)
    _assert_answer(result, calls, charges)
    successful = [c for c in charges if not c.ambiguous and c.tokens > 0]
    assert len(successful) >= 2  # real reported usage settled each request


async def test_overloaded_primary_falls_back_with_its_tool_history() -> None:
    settings = _settings()
    backup = openai_model(settings)
    assert backup is not None
    primary = FailAfter(_gemini(settings), ok=1)
    result, calls, charges = await _run([primary, backup], settings)
    # The tool ran once; the backup continued from Gemini's call and result.
    _assert_answer(result, calls, charges)
    messages = result.all_messages()
    providers = [m.provider_name for m in messages if isinstance(m, ModelResponse)]
    assert providers[0] == "google-interactions"
    assert providers[-1] == "openai"
    # The failed primary attempt was reserved and counted too.
    assert len(charges) >= 3
    assert any(
        isinstance(p, ToolCallPart)
        for m in messages
        if isinstance(m, ModelResponse)
        for p in m.parts
    )


async def test_gemini_continues_a_conversation_started_by_gpt() -> None:
    settings = _settings()
    backup = openai_model(settings)
    assert backup is not None
    # GPT first (one request), then Gemini receives GPT's function call.
    result, calls, charges = await _run(
        [FailAfter(backup, ok=1), _gemini(settings)], settings
    )
    _assert_answer(result, calls, charges)
    providers = [
        m.provider_name for m in result.all_messages() if isinstance(m, ModelResponse)
    ]
    assert providers == ["openai", "google-interactions"]
