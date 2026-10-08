"""Currency conversion of recorded evidence, producing new derived evidence.

The source evidence is never rewritten. The derived record keeps the original
columns and adds converted ones, and its provenance names the source currency,
the rate (value, effective date, source, method) and the evidence it came from,
so a report can cite the conversion and still show the original figures.

Refusals are explicit. An unverified source currency is never replaced by a
guess, an unavailable rate is explained instead of estimated, and a request
whose historical/current choice would change the numbers asks for that choice.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from retail_analytics.application.evidence import EvidenceRejected, EvidenceService
from retail_analytics.application.preferences import PreferenceStore
from retail_analytics.application.tools.context import (
    ExecutionContext,
    OperationContext,
)
from retail_analytics.domain.currency import DECLARED_STATEMENT, SourceCurrency
from retail_analytics.domain.evidence import (
    Evidence,
    EvidenceCell,
    EvidenceColumn,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    Provenance,
)
from retail_analytics.domain.exchange_rates import (
    ExchangeRate,
    InvalidRate,
    RateBasis,
    check_currency_code,
)
from retail_analytics.domain.preferences import EffectivePreferences, PreferenceKind

DERIVED_SUBJECT_PREFIX = "currency:"
MAX_COLUMNS = 8


class RateUnavailable(Exception):
    """The provider has no rate for this pair and date (not a transient fault)."""


class RateProviderFailure(Exception):
    """The provider could not answer (network, throttling, bad response)."""


class ExchangeRateProvider(Protocol):
    """Source of rates. ``on=None`` asks for the latest published rate."""

    async def rate(self, base: str, quote: str, *, on: date | None) -> ExchangeRate:
        """Raise ``RateUnavailable`` or ``RateProviderFailure``; never guess."""
        ...


class SourceCurrencyProvider(Protocol):
    """Verified currency of the dataset's amounts (T34 owns verification)."""

    async def source_currency(self, ctx: ExecutionContext) -> SourceCurrency: ...


class DisplayCurrencyProvider(Protocol):
    """The executive's saved or session display-currency preference, if any."""

    async def display_currency(self, ctx: ExecutionContext) -> str | None: ...


class UnverifiedSourceCurrency:
    """Default until dataset currency metadata is verified: always unknown."""

    async def source_currency(self, ctx: ExecutionContext) -> SourceCurrency:
        return SourceCurrency.unknown()


class DeclaredSourceCurrency:
    """The operator's configured currency: declared, never verified or inferred."""

    def __init__(self, code: str) -> None:
        self._currency = SourceCurrency.declared_by_operator(code)

    async def source_currency(self, ctx: ExecutionContext) -> SourceCurrency:
        return self._currency


class StoredDisplayCurrency:
    """Reads the display currency from saved preferences for a trusted context."""

    def __init__(self, store: PreferenceStore) -> None:
        self._store = store

    async def display_currency(self, ctx: ExecutionContext) -> str | None:
        stored = await self._store.list_preferences(
            ctx.executive_id, ctx.correlation.session_id
        )
        return EffectivePreferences.build(stored).value(PreferenceKind.DISPLAY_CURRENCY)


class Refusal(StrEnum):
    SOURCE_CURRENCY_UNKNOWN = "source_currency_unknown"
    TARGET_CURRENCY_MISSING = "target_currency_missing"
    ALREADY_IN_CURRENCY = "already_in_currency"
    BASIS_UNCLEAR = "basis_unclear"
    INVALID_REQUEST = "invalid_request"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    RATE_UNAVAILABLE = "rate_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class ConversionRefused(Exception):
    """A conversion was not produced; ``message`` is safe to show the model."""

    def __init__(self, reason: Refusal, message: str) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class ConversionRequest:
    source_evidence_id: str
    columns: tuple[str, ...]
    # Explicit target (a report- or turn-level instruction); else the preference.
    target_currency: str | None = None
    # None means "not stated": clarified when it would change the numbers.
    basis: RateBasis | None = None
    as_of: date | None = None


@dataclass(frozen=True, slots=True)
class ConversionResult:
    evidence: Evidence
    rate: ExchangeRate
    source_currency: str
    converted_columns: tuple[str, ...]
    basis: RateBasis
    source_declared: bool = False

    @property
    def disclosure(self) -> str:
        text = (
            f"Converted from {self.source_currency} to {self.rate.quote} using "
            f"the {self.basis.value} rate. {self.rate.describe()}. "
            "Original amounts are kept."
        )
        if self.source_declared:
            text += f" Note: {DECLARED_STATEMENT}."
        return text


class CurrencyConversionService:
    def __init__(
        self,
        evidence: EvidenceService,
        rates: ExchangeRateProvider,
        source: SourceCurrencyProvider,
        display: DisplayCurrencyProvider,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._evidence = evidence
        self._rates = rates
        self._source = source
        self._display = display
        self._clock = clock

    async def convert(
        self, ctx: OperationContext, request: ConversionRequest
    ) -> ConversionResult:
        execution = ctx.execution
        source = await self._source.source_currency(execution)
        if source.code is None:
            raise ConversionRefused(
                Refusal.SOURCE_CURRENCY_UNKNOWN,
                "The dataset's currency has not been verified, so amounts cannot "
                "be converted. Report them without a currency symbol and say the "
                "currency is not verified.",
            )
        target = await self._target(execution, request)
        if target == source.code:
            raise ConversionRefused(
                Refusal.ALREADY_IN_CURRENCY,
                f"The amounts are already in {target}; no conversion is needed.",
            )
        source_evidence = await self._source_evidence(execution, request)
        indexes = _amount_columns(source_evidence, request.columns)
        basis, on = self._basis(request, source_evidence)
        rate = await self._rate(source.code, target, on)
        content = _derived_content(source_evidence, indexes, rate, basis, source)
        try:
            recorded = await self._evidence.record(ctx, content)
        except EvidenceRejected as rejected:
            raise ConversionRefused(
                Refusal.EVIDENCE_UNAVAILABLE,
                "The converted result could not be recorded "
                f"({rejected.reason}); try a smaller source result.",
            ) from rejected
        return ConversionResult(
            recorded,
            rate,
            source.code,
            tuple(
                c.name
                for c in content.table.columns[
                    len(source_evidence.content.table.columns) :
                ]
            ),
            basis,
            source.declared,
        )

    async def _target(
        self, execution: ExecutionContext, request: ConversionRequest
    ) -> str:
        target = request.target_currency or await self._display.display_currency(
            execution
        )
        if target is None:
            raise ConversionRefused(
                Refusal.TARGET_CURRENCY_MISSING,
                "No target currency was given and no display currency is saved. "
                "Ask which currency to show.",
            )
        try:
            return check_currency_code(target)
        except InvalidRate:
            raise ConversionRefused(
                Refusal.INVALID_REQUEST, "The target currency must be an ISO 4217 code."
            ) from None

    async def _source_evidence(
        self, execution: ExecutionContext, request: ConversionRequest
    ) -> Evidence:
        usable = await self._evidence.usable_in_session(execution, limit=100)
        for record in usable:
            if record.evidence_id == request.source_evidence_id:
                return record
        raise ConversionRefused(
            Refusal.EVIDENCE_UNAVAILABLE,
            "That evidence is not available in this session; run the query again.",
        )

    def _basis(
        self, request: ConversionRequest, source: Evidence
    ) -> tuple[RateBasis, date | None]:
        today = self._clock().date()
        basis, as_of = request.basis, request.as_of
        if basis is RateBasis.HISTORICAL:
            if as_of is None or as_of > today:
                raise ConversionRefused(
                    Refusal.INVALID_REQUEST,
                    "A historical conversion needs a date that is not in the future.",
                )
            return basis, as_of
        if as_of is not None:
            raise ConversionRefused(
                Refusal.INVALID_REQUEST, "A date is only used for historical rates."
            )
        period = source.content.analysis.period
        if basis is None and period is not None and period.last_day < today:
            raise ConversionRefused(
                Refusal.BASIS_UNCLEAR,
                f"These amounts cover {period.start} to {period.last_day}. Ask "
                "whether to convert at the current rate or at the rate on a "
                "specific date (for example the period end); the figures differ.",
            )
        return RateBasis.CURRENT, None

    async def _rate(self, base: str, quote: str, on: date | None) -> ExchangeRate:
        try:
            rate = await self._rates.rate(base, quote, on=on)
        except RateUnavailable:
            when = f"on {on}" if on else "currently"
            raise ConversionRefused(
                Refusal.RATE_UNAVAILABLE,
                f"No {base} to {quote} rate is available {when}. Report the "
                "amounts in their original currency and say so.",
            ) from None
        except RateProviderFailure:
            raise ConversionRefused(
                Refusal.PROVIDER_UNAVAILABLE,
                "The exchange-rate provider is not responding. Report the "
                "amounts in their original currency and say conversion failed.",
            ) from None
        if (rate.base, rate.quote) != (base, quote):
            raise ConversionRefused(
                Refusal.PROVIDER_UNAVAILABLE,
                "The exchange-rate provider returned an unexpected pair.",
            )
        return rate


def _amount_columns(source: Evidence, names: Sequence[str]) -> tuple[int, ...]:
    table = source.content.table
    if not names or len(names) > MAX_COLUMNS or len(set(names)) != len(names):
        raise ConversionRefused(
            Refusal.INVALID_REQUEST,
            f"Name between 1 and {MAX_COLUMNS} distinct amount columns.",
        )
    existing = table.column_names
    indexes: list[int] = []
    for name in names:
        if name not in existing:
            raise ConversionRefused(
                Refusal.INVALID_REQUEST, f"Unknown column {name!r} in that evidence."
            )
        index = existing.index(name)
        if table.columns[index].role != "value" or not all(
            _is_number(row[index]) for row in table.rows
        ):
            raise ConversionRefused(
                Refusal.INVALID_REQUEST, f"Column {name!r} is not a numeric amount."
            )
        indexes.append(index)
    return tuple(indexes)


def _is_number(cell: EvidenceCell) -> bool:
    if cell is None:
        return True
    if isinstance(cell, bool):
        return False
    if isinstance(cell, float):
        return math.isfinite(cell)
    return isinstance(cell, int | Decimal) and Decimal(cell).is_finite()


def _decimal(cell: EvidenceCell) -> Decimal | None:
    if cell is None:
        return None
    return Decimal(repr(cell)) if isinstance(cell, float) else Decimal(str(cell))


def converted_name(column: str, quote: str) -> str:
    return f"{column}_{quote.lower()}"


def _derived_content(
    source: Evidence,
    indexes: tuple[int, ...],
    rate: ExchangeRate,
    basis: RateBasis,
    currency: SourceCurrency,
) -> EvidenceContent:
    table = source.content.table
    names = tuple(converted_name(table.columns[i].name, rate.quote) for i in indexes)
    if set(names) & set(table.column_names):
        raise ConversionRefused(
            Refusal.INVALID_REQUEST, "A converted column name would collide."
        )
    columns = table.columns + tuple(
        EvidenceColumn(n, "value", table.columns[i].sources)
        for n, i in zip(names, indexes, strict=True)
    )
    rows: list[tuple[EvidenceCell, ...]] = []
    for row in table.rows:
        extra: list[EvidenceCell] = []
        for i in indexes:
            amount = _decimal(row[i])
            extra.append(None if amount is None else rate.convert(amount))
        rows.append(row + tuple(extra))
    notes = (
        ("kind", "currency_conversion"),
        ("source_currency", currency.code or ""),
        (
            "source_currency_basis",
            DECLARED_STATEMENT if currency.declared else "verified dataset metadata",
        ),
        ("display_currency", rate.quote),
        ("rate", str(rate.rate)),
        ("rate_source", rate.source),
        ("rate_date", rate.effective_date.isoformat()),
        ("rate_method", rate.method),
        ("rate_basis", basis.value),
        (
            "rate_requested_date",
            rate.requested_date.isoformat() if rate.requested_date else "latest",
        ),
        ("rounding", "half up to the display currency's minor units"),
        ("converted_columns", ",".join(table.columns[i].name for i in indexes)),
    )
    key = "|".join(
        (
            source.evidence_id,
            ",".join(names),
            rate.quote,
            basis.value,
            rate.effective_date.isoformat(),
            str(rate.rate),
        )
    )
    return EvidenceContent(
        kind=EvidenceKind.DERIVED,
        subject_key=DERIVED_SUBJECT_PREFIX + hashlib.sha256(key.encode()).hexdigest(),
        analysis=source.content.analysis,
        provenance=Provenance(notes=notes),
        table=EvidenceTable(
            columns,
            tuple(rows),
            table.received_rows,
            table.truncation,
            table.masked_cells,
        ),
        grain=source.content.grain,
        analytical_slots=source.content.analytical_slots,
        derived_from=(source.evidence_id,),
    )
