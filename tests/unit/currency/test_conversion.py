"""Conversion service and capability: disclosure, refusals, evidence, authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from retail_analytics.adapters.exchange_rates.fixture import FixtureRateProvider
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.currency_conversion import (
    ConversionRefused,
    ConversionRequest,
    ConversionResult,
    CurrencyConversionService,
    RateProviderFailure,
    Refusal,
    StoredDisplayCurrency,
    UnverifiedSourceCurrency,
)
from retail_analytics.application.tools import (
    CapabilityRegistry,
    OperationContext,
    ToolCall,
    ToolFailed,
    ToolSucceeded,
    invoke,
)
from retail_analytics.capabilities.currency import (
    CONVERT_CURRENCY,
    ConvertCurrencyOutput,
    currency_capability,
)
from retail_analytics.domain.currency import SourceCurrency
from retail_analytics.domain.evidence import Evidence
from retail_analytics.domain.exchange_rates import RateBasis
from retail_analytics.domain.operations import ToolErrorCode
from retail_analytics.domain.periods import OverrideScope
from retail_analytics.domain.preferences import (
    PreferenceKind,
    PreferenceSetting,
    PreferenceSource,
)
from tests.unit.evidence.support import (
    EXEC_A,
    EXEC_B,
    SCOPE_B,
    Env,
    basis,
    compiled_and_released,
    context,
    operation,
)
from tests.unit.preferences.fakes import FakePreferenceStore
from tests.unit.tools.fakes import RecordingSink

pytestmark = pytest.mark.asyncio

RATES = {
    ("USD", "EUR"): {
        date(2026, 9, 30): Decimal("0.845"),
        date(2026, 10, 8): Decimal("0.9"),
    }
}


@dataclass
class FixedSource:
    currency: SourceCurrency

    async def source_currency(self, ctx: ExecutionContext) -> SourceCurrency:
        return self.currency


@dataclass
class FixedDisplay:
    code: str | None = None

    async def display_currency(self, ctx: ExecutionContext) -> str | None:
        return self.code


@dataclass
class Harness:
    env: Env = field(default_factory=Env)
    rates: FixtureRateProvider = field(
        default_factory=lambda: FixtureRateProvider(RATES)
    )
    source: SourceCurrency = field(default_factory=lambda: SourceCurrency("USD"))
    display: str | None = None
    service: CurrencyConversionService = field(init=False)

    def __post_init__(self) -> None:
        self.service = CurrencyConversionService(
            self.env.service,
            self.rates,
            FixedSource(self.source),
            FixedDisplay(self.display),
            clock=self.env.clock,
        )

    async def evidence(self, op: str = "op-q", **overrides: Any) -> Evidence:
        compiled, released = compiled_and_released()
        return await self.env.service.record_query(
            operation(context(), op), compiled, released, basis(**overrides)
        )

    async def convert(
        self, source: Evidence, op: str = "op-c", **kw: Any
    ) -> ConversionResult:
        params: dict[str, Any] = {
            "source_evidence_id": source.evidence_id,
            "columns": ("spend",),
            "target_currency": "EUR",
            "basis": RateBasis.CURRENT,
        }
        params.update(kw)
        return await self.service.convert(
            operation(context(), op), ConversionRequest(**params)
        )

    def derived(self) -> list[Evidence]:
        return [r for r in self.env.store.records.values() if r.content.derived_from]


async def test_converts_keeps_original_and_discloses_the_rate() -> None:
    h = Harness()
    source = await h.evidence()
    result = await h.convert(source)
    table = result.evidence.content.table
    assert table.column_names == ("customer_ref", "spend", "spend_eur")
    assert [r[1] for r in table.rows] == [r[1] for r in source.content.table.rows]
    # 0.9 current rate; 12 -> 10.80, 45 -> 40.50, 60 -> 54.00, 5 -> 4.50
    assert {r[1]: r[2] for r in table.rows} == {
        12.0: Decimal("10.80"),
        45.0: Decimal("40.50"),
        60.0: Decimal("54.00"),
        5.0: Decimal("4.50"),
    }
    notes = dict(result.evidence.content.provenance.notes)
    assert notes["rate"] == "0.9"
    assert notes["rate_date"] == "2026-10-08"
    assert notes["rate_source"] == "fixture rates"
    assert notes["rate_method"]
    assert notes["source_currency"] == "USD"
    assert notes["rate_basis"] == "current"
    assert "1 USD = 0.9 EUR" in result.disclosure
    assert "Original amounts are kept" in result.disclosure


async def test_derived_evidence_links_to_an_unchanged_source() -> None:
    h = Harness()
    source = await h.evidence()
    result = await h.convert(source)
    derived = result.evidence
    assert derived.content.derived_from == (source.evidence_id,)
    assert derived.evidence_id != source.evidence_id
    stored = h.env.store.records[source.evidence_id]
    assert stored == source and stored.is_intact
    assert source.content.table.column_names == ("customer_ref", "spend")
    assert derived.content.analysis == source.content.analysis
    assert derived.content.grain == source.content.grain


async def test_historical_rate_uses_the_stated_date_and_discloses_it() -> None:
    h = Harness()
    source = await h.evidence()
    result = await h.convert(
        source, basis=RateBasis.HISTORICAL, as_of=date(2026, 10, 3)
    )
    assert result.rate.effective_date == date(2026, 9, 30)
    assert {r[1]: r[2] for r in result.evidence.content.table.rows}[5.0] == Decimal(
        "4.23"
    )
    assert "on or before 2026-10-03" in result.disclosure
    assert h.rates.calls == [("USD", "EUR", date(2026, 10, 3))]


async def test_ambiguous_basis_for_a_past_period_asks_for_clarification() -> None:
    h = Harness()
    source = await h.evidence()  # September 2026; today is 2026-10-08
    with pytest.raises(ConversionRefused) as caught:
        await h.convert(source, basis=None)
    assert caught.value.reason is Refusal.BASIS_UNCLEAR
    assert "2026-09-30" in caught.value.message
    assert h.rates.calls == []
    no_period = await h.evidence("op-q2", period=None)
    result = await h.convert(no_period, op="op-c2", basis=None)
    assert result.basis is RateBasis.CURRENT


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"basis": RateBasis.HISTORICAL}, Refusal.INVALID_REQUEST),
        (
            {"basis": RateBasis.HISTORICAL, "as_of": date(2026, 10, 9)},
            Refusal.INVALID_REQUEST,
        ),
        ({"as_of": date(2026, 10, 1)}, Refusal.INVALID_REQUEST),
        ({"columns": ("nope",)}, Refusal.INVALID_REQUEST),
        ({"columns": ("customer_ref",)}, Refusal.INVALID_REQUEST),
        ({"columns": ()}, Refusal.INVALID_REQUEST),
        ({"columns": ("spend", "spend")}, Refusal.INVALID_REQUEST),
        ({"target_currency": "USD"}, Refusal.ALREADY_IN_CURRENCY),
        ({"target_currency": "eur"}, Refusal.INVALID_REQUEST),
        ({"target_currency": None}, Refusal.TARGET_CURRENCY_MISSING),
        ({"target_currency": "ARS"}, Refusal.RATE_UNAVAILABLE),
        ({"source_evidence_id": "evd-missing"}, Refusal.EVIDENCE_UNAVAILABLE),
    ],
)
async def test_invalid_requests_are_refused_without_a_conversion(
    kw: dict[str, Any], reason: Refusal
) -> None:
    h = Harness()
    source = await h.evidence()
    with pytest.raises(ConversionRefused) as caught:
        await h.convert(source, **kw)
    assert caught.value.reason is reason
    assert not h.derived()


@pytest.mark.parametrize("source", [SourceCurrency.unknown(), SourceCurrency()])
async def test_unknown_source_currency_is_never_guessed(
    source: SourceCurrency,
) -> None:
    h = Harness(source=source)
    evidence = await h.evidence()
    with pytest.raises(ConversionRefused) as caught:
        await h.convert(evidence)
    assert caught.value.reason is Refusal.SOURCE_CURRENCY_UNKNOWN
    assert "not been verified" in caught.value.message
    assert h.rates.calls == []
    assert not h.derived()


async def test_default_source_provider_is_unverified() -> None:
    known = (await UnverifiedSourceCurrency().source_currency(context())).is_known
    assert not known


async def test_provider_faults_are_explained_not_estimated() -> None:
    h = Harness()
    source = await h.evidence()
    h.rates.fail_with = RateProviderFailure("down")
    with pytest.raises(ConversionRefused) as caught:
        await h.convert(source)
    assert caught.value.reason is Refusal.PROVIDER_UNAVAILABLE
    assert not h.derived()


async def test_display_currency_preference_is_the_default_target() -> None:
    h = Harness(display="EUR")
    source = await h.evidence()
    result = await h.convert(source, target_currency=None)
    assert result.rate.quote == "EUR"


async def test_preference_adapter_applies_the_explicit_saved_currency() -> None:
    store = FakePreferenceStore()
    adapter = StoredDisplayCurrency(store)
    assert await adapter.display_currency(context()) is None
    await store.save(
        EXEC_A,
        PreferenceSetting(PreferenceKind.DISPLAY_CURRENCY, "GBP"),
        OverrideScope.USER_DEFAULT,
        None,
        PreferenceSource.EXPLICIT,
    )
    await store.save(
        EXEC_A,
        PreferenceSetting(PreferenceKind.DISPLAY_CURRENCY, "EUR"),
        OverrideScope.SESSION,
        "ses-a",
        PreferenceSource.EXPLICIT,
    )
    assert await adapter.display_currency(context()) == "EUR"
    assert await adapter.display_currency(context(session="ses-other")) == "GBP"
    assert await adapter.display_currency(context(EXEC_B, SCOPE_B)) is None


async def test_another_executive_cannot_convert_this_evidence() -> None:
    h = Harness()
    source = await h.evidence()
    other = context(EXEC_B, SCOPE_B, session="ses-b", run="run-b")
    with pytest.raises(ConversionRefused) as caught:
        await h.service.convert(
            operation(other, "op-x"),
            ConversionRequest(source.evidence_id, ("spend",), "EUR", RateBasis.CURRENT),
        )
    assert caught.value.reason is Refusal.EVIDENCE_UNAVAILABLE


def invocation(h: Harness, source: Evidence, **extra: Any) -> Any:
    registry = CapabilityRegistry((currency_capability(h.service),))
    arguments = {
        "evidence_id": source.evidence_id,
        "amount_columns": ["spend"],
        "target_currency": "EUR",
        **extra,
    }
    return invoke(
        registry,
        ToolCall(call_id="c-1", name=CONVERT_CURRENCY, arguments=arguments),
        OperationContext(execution=context(), operation_id="op-cap"),
        RecordingSink(),
    )


async def test_capability_returns_originals_rate_and_evidence_id() -> None:
    h = Harness()
    source = await h.evidence()
    registry = CapabilityRegistry((currency_capability(h.service),))
    assert [t.name for t in registry.catalog(context())] == [CONVERT_CURRENCY]
    result = await invocation(h, source, rate_basis="current")
    assert isinstance(result.outcome, ToolSucceeded)
    out = result.outcome.output
    assert isinstance(out, ConvertCurrencyOutput)
    assert out.columns == ("customer_ref", "spend", "spend_eur")
    assert out.converted_columns == ("spend_eur",)
    assert out.rate == "0.9" and out.rate_date == date(2026, 10, 8)
    assert out.rate_source and out.rate_method and out.display_currency == "EUR"
    assert (5.0, "4.50") in [r[1:] for r in out.rows]
    assert out.evidence_id in h.env.store.records


async def test_capability_maps_refusals_to_tool_errors() -> None:
    unknown = Harness(source=SourceCurrency.unknown())
    result = await invocation(unknown, await unknown.evidence())
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.FIELD_UNAVAILABLE
    assert "not been verified" in result.outcome.message

    h = Harness()
    unclear = await invocation(h, await h.evidence())
    assert isinstance(unclear.outcome, ToolFailed)
    assert unclear.outcome.code is ToolErrorCode.INVALID_INPUT
    assert "current rate" in unclear.outcome.message

    h.rates.fail_with = RateProviderFailure("down")
    down = await invocation(h, await h.evidence("op-q2"), rate_basis="current")
    assert isinstance(down.outcome, ToolFailed)
    assert down.outcome.code is ToolErrorCode.TEMPORARY_FAILURE


async def test_capability_rejects_trusted_or_extra_arguments() -> None:
    h = Harness()
    result = await invocation(h, await h.evidence(), source_currency="USD")
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.INVALID_INPUT
    assert not h.derived() and h.rates.calls == []


async def test_bootstrap_default_refuses_until_the_source_currency_is_verified() -> (
    None
):
    from retail_analytics.bootstrap.config import BackendSettings
    from retail_analytics.bootstrap.currency import build_currency_conversion

    env = Env()
    rates = FixtureRateProvider(RATES)
    service = build_currency_conversion(
        BackendSettings(), env.service, FakePreferenceStore(), rates=rates
    )
    compiled, released = compiled_and_released()
    source = await env.service.record_query(
        operation(context(), "op-q"), compiled, released, basis()
    )
    with pytest.raises(ConversionRefused) as caught:
        await service.convert(
            operation(context(), "op-c"),
            ConversionRequest(source.evidence_id, ("spend",), "EUR", RateBasis.CURRENT),
        )
    assert caught.value.reason is Refusal.SOURCE_CURRENCY_UNKNOWN
    assert rates.calls == []


async def test_declared_source_currency_is_disclosed_as_not_verified() -> None:
    from retail_analytics.application.currency_conversion import (
        DeclaredSourceCurrency,
    )
    from retail_analytics.bootstrap.config import BackendSettings
    from retail_analytics.bootstrap.currency import build_currency_conversion

    statement = "source currency declared by operator, not verified from data"
    env = Env()
    service = build_currency_conversion(
        BackendSettings(source_currency_declared="USD"),
        env.service,
        FakePreferenceStore(),
        rates=FixtureRateProvider(RATES),
    )
    declared = await DeclaredSourceCurrency("USD").source_currency(context())
    assert declared.declared and declared.code == "USD"
    assert statement in declared.label()
    compiled, released = compiled_and_released()
    source = await env.service.record_query(
        operation(context(), "op-q"), compiled, released, basis()
    )
    result = await service.convert(
        operation(context(), "op-c"),
        ConversionRequest(source.evidence_id, ("spend",), "EUR", RateBasis.CURRENT),
    )
    notes = dict(result.evidence.content.provenance.notes)
    assert notes["source_currency"] == "USD"
    assert notes["source_currency_basis"] == statement
    assert "verified dataset metadata" not in notes.values()
    assert statement in result.disclosure


async def test_verified_source_has_no_declared_statement() -> None:
    h = Harness()
    result = await h.convert(await h.evidence())
    assert "declared" not in result.disclosure
