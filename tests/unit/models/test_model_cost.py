"""Estimated model spend per question and its soft dollar limit (T30-F3).

Fixed test prices (operator overrides, USD per million tokens) make every
expected cost exact: Gemini input 1, cached input 0.1, output 10; GPT input 2,
cached input 0.5, output 8. One micro-dollar is one token at USD 1/M.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic_ai.usage import RequestUsage

from retail_analytics.adapters.models.budgeted import reported_usage
from retail_analytics.adapters.models.gemini_interactions import interaction_usage
from retail_analytics.adapters.models.pricing import GenaiPricesCatalog, PriceOverride
from retail_analytics.adapters.telemetry.otel import mlflow_usage_attributes
from retail_analytics.application.budgets import RunBudgets, budget_message
from retail_analytics.application.contracts.budgets import ProviderUsage
from retail_analytics.application.contracts.investigations import StopReason
from retail_analytics.application.contracts.model_costs import ModelRef
from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.investigation_runtime import (
    RunStopped,
    stop_message,
)
from retail_analytics.application.telemetry import use_telemetry
from retail_analytics.bootstrap.budgets import model_pricing, run_limits
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.domain.budgets import BudgetExhausted, BudgetResource, RunLimits
from tests.unit.budgets.memory_store import MemoryRunBudgetStore
from tests.unit.models import stubs
from tests.unit.models.test_provider_chain import gemini_answer, gpt_answer, harness
from tests.unit.telemetry.recording import recording

NOW = datetime(2026, 10, 9, tzinfo=UTC)
GEMINI = "google-interactions"
PRICES = GenaiPricesCatalog(
    {
        "gemini-3.8-flash": PriceOverride(Decimal(1), Decimal(10), Decimal("0.1")),
        "gpt-5-mini": PriceOverride(Decimal(2), Decimal(8), Decimal("0.5")),
    }
)


def tool_call(**usage: int) -> stubs.Reply:
    return stubs.Reply(
        events=stubs.gemini_call(
            "execute_analysis", {"sql": "S"}, usage=stubs.gemini_usage(**usage)
        )
    )


# -- usage normalization and prices -------------------------------------------


def test_gemini_thoughts_are_output_and_cached_input_is_part_of_input() -> None:
    usage = interaction_usage(
        {
            "total_input_tokens": 100,
            "total_cached_tokens": 40,
            "total_output_tokens": 20,
            "total_thought_tokens": 5,
            "total_tool_use_tokens": 0,
        }
    )
    # Token budget unchanged: input 100, output 20 + 5 thoughts, added once.
    assert (usage.input_tokens, usage.output_tokens) == (100, 25)
    provider = reported_usage(usage)
    assert provider == ProviderUsage(100, 25, 40, 0, 5)
    estimate = PRICES.estimate(ModelRef(GEMINI, "gemini-3.8-flash"), provider, at=NOW)
    assert estimate is not None
    # 60 uncached x 1 + 40 cached x 0.1 + 25 output x 10, per million.
    assert estimate.total_usd * 1_000_000 == 314


def test_openai_reasoning_is_inside_output_and_never_charged_twice() -> None:
    usage = RequestUsage(
        input_tokens=50,
        cache_read_tokens=20,
        output_tokens=10,
        details={"reasoning_tokens": 4},
    )
    provider = reported_usage(usage)
    assert provider.reasoning_tokens == 4 and provider.total == 60
    estimate = PRICES.estimate(ModelRef("openai", "gpt-5-mini"), provider, at=NOW)
    assert estimate is not None
    assert estimate.total_usd * 1_000_000 == 30 * 2 + 20 * Decimal("0.5") + 10 * 8


def test_the_maintained_price_list_applies_cache_prices_and_dated_changes() -> None:
    catalog = GenaiPricesCatalog()
    ref = ModelRef(GEMINI, "gemini-3.8-flash")
    usage = ProviderUsage(1_000_000, 1_000_000, cached_input_tokens=0)
    now = catalog.estimate(ref, usage, at=NOW)
    later = catalog.estimate(ref, usage, at=datetime(2027, 2, 1, tzinfo=UTC))
    assert now is not None and later is not None
    # Published standard rates: 0.75/3.75 through 2026, then 1.50/7.50.
    assert (now.input_usd, now.output_usd) == (Decimal("0.75"), Decimal("3.75"))
    assert (later.input_usd, later.output_usd) == (Decimal("1.5"), Decimal("7.5"))
    assert now.basis.source == "genai-prices" and not now.basis.overridden
    assert "google/gemini-3.8-flash" in now.basis.version
    cached = catalog.estimate(
        ref, ProviderUsage(1_000_000, 0, cached_input_tokens=1_000_000), at=NOW
    )
    assert cached is not None and cached.input_usd == Decimal("0.075")
    assert catalog.basis(ModelRef("openai", "gpt-5-mini"), at=NOW) is not None


def test_unknown_models_have_no_price_and_local_models_are_free() -> None:
    catalog = GenaiPricesCatalog()
    assert catalog.basis(ModelRef(GEMINI, "gemini-unknown-x"), at=NOW) is None
    assert catalog.basis(ModelRef("acme", "gpt-5-mini"), at=NOW) is None
    assert catalog.estimate(ModelRef(GEMINI, "x"), ProviderUsage(1, 1), at=NOW) is None
    free = catalog.estimate(
        ModelRef("function", "fixture"), ProviderUsage(9, 9), at=NOW
    )
    assert free is not None and free.total_usd == 0
    overridden = PRICES.basis(ModelRef(GEMINI, "gemini-3.8-flash"), at=NOW)
    assert overridden is not None and overridden.overridden


def test_settings_set_the_default_limit_and_parse_overrides() -> None:
    settings = load_backend_settings(
        {
            "APP_MODE": "fixture",
            "MODEL_PRICE_OVERRIDES": '{"m-1": {"input": 1, "output": 2}}',
        },
        env_file=None,
    )
    assert run_limits(settings).model_cost_micros == 1_000_000
    estimate = model_pricing(settings).estimate(
        ModelRef(GEMINI, "m-1"), ProviderUsage(10, 10, cached_input_tokens=5), at=NOW
    )
    # Without a cached price, cached input is charged as ordinary input.
    assert estimate is not None and estimate.total_usd * 1_000_000 == 30
    off = load_backend_settings(
        {"APP_MODE": "fixture", "RUN_MAX_MODEL_COST_USD": "0"}, env_file=None
    )
    assert run_limits(off).model_cost_micros == 0


# -- accounting through the real provider chain --------------------------------


@pytest.mark.asyncio
async def test_each_attempt_is_priced_once_with_its_usage_and_basis() -> None:
    gemini = stubs.Recorder([tool_call(cached_tokens=40), gemini_answer()])
    h = harness(gemini, None, pricing=PRICES)
    await h.run()
    first, second = await h.provider_charges()
    assert (first.cost_micros, second.cost_micros) == (314, 350)
    assert first.detail is not None
    assert first.detail["cached_input_tokens"] == 40
    assert first.detail["reasoning_tokens"] == 5
    assert first.detail["output_tokens"] == 25
    assert first.detail["price_source"] == "override"
    budget = await h.store.get("run-1")
    assert budget is not None and budget.usage.model_cost_micros == 664


@pytest.mark.asyncio
async def test_retries_and_fallback_are_each_charged_unknown_usage_estimated() -> None:
    gemini = stubs.Recorder([stubs.error(503, "unavailable"), stubs.error(400, "bad")])
    gpt = stubs.Recorder(
        [
            stubs.Reply(
                events=stubs.openai_call(
                    "final_result",
                    {"text": "Sales were 10."},
                    tokens=50,
                    cached_tokens=20,
                    reasoning_tokens=4,
                )
            )
        ]
    )
    h = harness(gemini, gpt, pricing=PRICES)
    await h.run()
    retried, rejected, answered = await h.provider_charges()
    # The 503 reported no usage: its input estimate is priced (a lower bound).
    assert retried.ambiguous and retried.cost_micros == retried.tokens > 0
    assert retried.detail is not None and retried.detail["usage_reported"] is False
    assert rejected.cost_micros == 0  # a definite rejection used nothing
    assert answered.cost_micros == 150 and answered.detail is not None
    assert answered.detail["reasoning_tokens"] == 4
    budget = await h.store.get("run-1")
    assert budget is not None
    assert budget.usage.model_cost_micros == retried.tokens + 150


@pytest.mark.asyncio
async def test_crossing_the_limit_stops_the_next_request_not_the_current_one() -> None:
    gemini = stubs.Recorder([tool_call(), tool_call(), gemini_answer()])
    gpt = stubs.Recorder([gpt_answer()])
    h = harness(gemini, gpt, pricing=PRICES, limits=RunLimits(model_cost_micros=500))
    with pytest.raises(RunStopped) as stopped:
        await h.run()
    assert stopped.value.reason is StopReason.BUDGET
    assert stopped.value.resource is BudgetResource.MODEL_COST
    # 350 < 500 lets the second request go; it overshoots to 700 (soft limit).
    assert len(gemini.requests) == 2 and gpt.requests == []
    budget = await h.store.get("run-1")
    assert budget is not None and budget.usage.model_cost_micros == 700
    assert "spending limit" in stop_message(
        StopReason.BUDGET, BudgetResource.MODEL_COST
    )


@pytest.mark.asyncio
async def test_a_retry_after_crossing_the_limit_is_never_sent() -> None:
    gemini = stubs.Recorder([stubs.error(503, "unavailable"), gemini_answer()])
    h = harness(gemini, None, pricing=PRICES, limits=RunLimits(model_cost_micros=1))
    with pytest.raises(RunStopped) as stopped:
        await h.run()
    assert stopped.value.resource is BudgetResource.MODEL_COST
    assert len(gemini.requests) == 1


@pytest.mark.asyncio
async def test_a_fallback_after_crossing_the_limit_is_never_sent() -> None:
    gemini = stubs.Recorder([stubs.error(503, "unavailable")])
    gpt = stubs.Recorder([gpt_answer()])
    limits = RunLimits(model_cost_micros=1, transient_attempts=1)
    h = harness(gemini, gpt, pricing=PRICES, limits=limits)
    with pytest.raises(RunStopped) as stopped:
        await h.run()
    assert stopped.value.resource is BudgetResource.MODEL_COST
    assert len(gemini.requests) == 1 and gpt.requests == []


@pytest.mark.asyncio
async def test_an_unknown_price_stops_before_any_paid_request() -> None:
    gemini = stubs.Recorder([gemini_answer()])
    h = harness(
        gemini,
        None,
        pricing=GenaiPricesCatalog(),
        limits=RunLimits(model_cost_micros=1_000_000),
        gemini_model="gemini-unknown-x",
    )
    with pytest.raises(RunStopped) as stopped:
        await h.run()
    assert stopped.value.resource is BudgetResource.MODEL_PRICE
    assert gemini.requests == [] and await h.provider_charges() == []
    assert "price" in budget_message(BudgetResource.MODEL_PRICE)


@pytest.mark.asyncio
async def test_without_a_dollar_limit_unknown_cost_is_recorded_not_zero() -> None:
    gemini = stubs.Recorder([gemini_answer()])
    h = harness(
        gemini, None, pricing=GenaiPricesCatalog(), gemini_model="gemini-unknown-x"
    )
    await h.run()
    (charge,) = await h.provider_charges()
    assert charge.settled and charge.cost_micros is None
    budget = await h.store.get("run-1")
    assert budget is not None
    assert (budget.usage.model_cost_micros, budget.usage.unpriced_requests) == (0, 1)


@pytest.mark.asyncio
async def test_spend_survives_restart_and_replayed_settlement_counts_once() -> None:
    store = MemoryRunBudgetStore()
    limits = RunLimits(model_cost_micros=500)
    first = RunBudgets(store, limits, clock=lambda: NOW, pricing=PRICES)
    ref = ModelRef(GEMINI, "gemini-3.8-flash")
    await first.reserve_provider_request(
        "run", "k1", estimated_input_tokens=10, model=ref
    )
    for _ in range(2):  # a retried activity replays the settlement
        await first.record_provider_usage(
            "run", "k1", ProviderUsage(100, 50), model=ref
        )
    # A new process with a larger configured limit keeps the pinned one.
    later = RunBudgets(
        store, RunLimits(model_cost_micros=10**9), clock=lambda: NOW, pricing=PRICES
    )
    snapshot = await later.snapshot("run")
    assert snapshot is not None and snapshot.usage.model_cost_micros == 600
    with pytest.raises(BudgetExhausted) as error:
        await later.reserve_provider_request(
            "run", "k2", estimated_input_tokens=10, model=ref
        )
    assert error.value.resource is BudgetResource.MODEL_COST


# -- telemetry ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_attempt_spans_carry_cost_and_metrics_have_no_run_labels() -> None:
    telemetry, sink = recording()
    gemini = stubs.Recorder([tool_call(cached_tokens=40), gemini_answer()])
    with use_telemetry(telemetry):
        await harness(gemini, None, pricing=PRICES).run()
    first, second = sink.named(Span.MODEL_ATTEMPT)
    assert first.attributes["cost_status"] == "estimated"
    assert first.attributes["cost_usd"] == pytest.approx(0.000314)
    assert first.attributes["usage_cached_input_tokens"] == 40
    assert first.attributes["usage_thinking_tokens"] == 5
    assert first.attributes["price_source"] == "override"
    assert second.attributes["cost_usd"] == pytest.approx(0.00035)
    # The logical request span carries no cost: MLflow sums attempts only.
    assert all("cost_usd" not in s.attributes for s in sink.named(Span.MODEL_REQUEST))
    assert sink.total(Metric.MODEL_COST, provider=GEMINI) == pytest.approx(0.000664)
    for metric, _, labels in sink.counts:
        if metric is Metric.MODEL_COST:
            assert set(labels) <= {"provider", "model", "kind"}


@pytest.mark.asyncio
async def test_unknown_cost_is_visible_and_never_reported_as_zero() -> None:
    telemetry, sink = recording()
    gemini = stubs.Recorder([gemini_answer()])
    with use_telemetry(telemetry):
        await harness(
            gemini, None, pricing=GenaiPricesCatalog(), gemini_model="gemini-unknown-x"
        ).run()
    (attempt,) = sink.named(Span.MODEL_ATTEMPT)
    assert attempt.attributes["cost_status"] == "unknown"
    assert "cost_usd" not in attempt.attributes
    assert sink.total(Metric.MODEL_UNPRICED, provider=GEMINI) == 1
    assert sink.total(Metric.MODEL_COST) == 0
    mlflow = mlflow_usage_attributes(attempt.attributes)
    assert "mlflow.llm.cost" not in mlflow
    assert '"input_tokens": 100' in mlflow["mlflow.chat.tokenUsage"]


def test_mlflow_cost_attributes_follow_its_documented_format() -> None:
    found = mlflow_usage_attributes(
        {
            "usage_input_tokens": 100,
            "usage_output_tokens": 25,
            "usage_cached_input_tokens": 40,
            "cost_usd": 0.000314,
            "cost_input_usd": 0.000064,
            "cost_output_usd": 0.00025,
        }
    )
    assert found["mlflow.chat.tokenUsage"] == (
        '{"input_tokens": 100, "output_tokens": 25, "total_tokens": 125, '
        '"cache_read_input_tokens": 40}'
    )
    assert found["mlflow.llm.cost"] == (
        '{"input_cost": 6.4e-05, "output_cost": 0.00025, "total_cost": 0.000314}'
    )


def test_run_total_marks_incomplete_cost_and_counts_overruns() -> None:
    from retail_analytics.application.investigation_runtime import (
        _model_cost_summary,
    )
    from retail_analytics.domain.budgets import BudgetSnapshot, RunUsage

    telemetry, sink = recording()
    limits = RunLimits(model_cost_micros=500)
    over = BudgetSnapshot("r", limits, RunUsage(model_cost_micros=700), NOW)
    partial = BudgetSnapshot(
        "r", limits, RunUsage(model_cost_micros=100, unpriced_requests=1), NOW
    )
    with use_telemetry(telemetry):
        summary = _model_cost_summary(over)
        unknown = _model_cost_summary(partial)
    assert summary == {
        "model_cost_usd": 0.0007,
        "model_cost_complete": True,
        "model_cost_unpriced_requests": 0,
        "model_cost_limit_usd": 0.0005,
    }
    assert unknown["model_cost_complete"] is False
    assert sink.total(Metric.MODEL_COST_OVERRUNS) == 1
    outcomes = [
        labels
        for metric, _, labels in sink.observations
        if metric is Metric.RUN_MODEL_COST
    ]
    assert [o[Label.OUTCOME] for o in outcomes] == ["complete", "incomplete"]
    assert over.exhausted() >= {BudgetResource.MODEL_COST}
    assert partial.exhausted() >= {BudgetResource.MODEL_PRICE}
