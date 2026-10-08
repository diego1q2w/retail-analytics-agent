"""Composition of currency conversion (wired into a runtime by its root)."""

from __future__ import annotations

from retail_analytics.adapters.exchange_rates.frankfurter import (
    FrankfurterRateProvider,
)
from retail_analytics.adapters.postgres.database import Clock, utc_now
from retail_analytics.application.currency_conversion import (
    CurrencyConversionService,
    ExchangeRateProvider,
    SourceCurrencyProvider,
    StoredDisplayCurrency,
    UnverifiedSourceCurrency,
)
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.preferences import PreferenceStore
from retail_analytics.bootstrap.config import BackendSettings


def build_currency_conversion(
    settings: BackendSettings,
    evidence: EvidenceService,
    preferences: PreferenceStore,
    *,
    rates: ExchangeRateProvider | None = None,
    source: SourceCurrencyProvider | None = None,
    clock: Clock = utc_now,
) -> CurrencyConversionService:
    """Pass ``rates`` to replace the live provider (tests, offline runs).

    Until dataset currency metadata is verified, ``source`` defaults to the
    unknown currency and every conversion is refused.
    """
    return CurrencyConversionService(
        evidence,
        rates or FrankfurterRateProvider(base_url=settings.exchange_rate_base_url),
        source or UnverifiedSourceCurrency(),
        StoredDisplayCurrency(preferences),
        clock=clock,
    )
