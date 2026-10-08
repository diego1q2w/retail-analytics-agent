"""The ``convert_currency`` capability.

Converts amount columns of recorded evidence into another currency and returns
the converted figures next to the originals, the rate used and its source,
date and method. The result is also recorded as derived evidence, so reports
cite the conversion while the original evidence stays untouched.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from pydantic import Field, StringConstraints

from retail_analytics.application.contracts import Identifier
from retail_analytics.application.currency_conversion import (
    ConversionRefused,
    ConversionRequest,
    CurrencyConversionService,
    Refusal,
)
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilitySpec,
    OperationContext,
    RetrySpec,
    ToolFailed,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSucceeded,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.exchange_rates import RateBasis
from retail_analytics.domain.operations import (
    RecoveryMode,
    SideEffect,
    ToolErrorCode,
)

CONVERT_CURRENCY = "convert_currency"

CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
ColumnName = Annotated[str, StringConstraints(min_length=1, max_length=128)]

_ERROR_CODES = {
    Refusal.SOURCE_CURRENCY_UNKNOWN: ToolErrorCode.FIELD_UNAVAILABLE,
    Refusal.RATE_UNAVAILABLE: ToolErrorCode.FIELD_UNAVAILABLE,
    Refusal.PROVIDER_UNAVAILABLE: ToolErrorCode.TEMPORARY_FAILURE,
    Refusal.EVIDENCE_UNAVAILABLE: ToolErrorCode.INVALID_INPUT,
    Refusal.TARGET_CURRENCY_MISSING: ToolErrorCode.INVALID_INPUT,
    Refusal.ALREADY_IN_CURRENCY: ToolErrorCode.INVALID_INPUT,
    Refusal.BASIS_UNCLEAR: ToolErrorCode.INVALID_INPUT,
    Refusal.INVALID_REQUEST: ToolErrorCode.INVALID_INPUT,
}


class ConvertCurrencyInput(ToolInput):
    evidence_id: Identifier = Field(
        description="Evidence id of the query result that holds the amounts."
    )
    amount_columns: tuple[ColumnName, ...] = Field(
        min_length=1,
        max_length=8,
        description="Numeric amount columns to convert; originals are kept.",
    )
    target_currency: CurrencyCode | None = Field(
        default=None,
        description=(
            "ISO 4217 code to convert to. Omit to use the saved display currency."
        ),
    )
    rate_basis: RateBasis | None = Field(
        default=None,
        description=(
            "'current' for today's rate or 'historical' for the rate on as_of. "
            "Omit only if the user has not said; the tool asks when it matters."
        ),
    )
    as_of: date | None = Field(
        default=None, description="Rate date, only with rate_basis 'historical'."
    )


class ConvertCurrencyOutput(ToolOutput):
    evidence_id: str
    source_currency: str
    display_currency: str
    rate: str
    rate_date: date
    rate_source: str
    rate_method: str
    rate_basis: str
    # Plain-language statement to include wherever converted figures appear.
    disclosure: str
    # All source columns, then the converted ones (originals are kept).
    columns: tuple[str, ...]
    converted_columns: tuple[str, ...]
    rows: tuple[tuple[str | int | float | None, ...], ...]
    truncated: bool


def _cell(value: object) -> str | int | float | None:
    if value is None or (
        isinstance(value, int | float) and not isinstance(value, bool)
    ):
        return value
    return str(value)


def currency_capability(
    service: CurrencyConversionService,
) -> CapabilitySpec[ConvertCurrencyInput, ConvertCurrencyOutput]:
    async def convert_currency(
        args: ConvertCurrencyInput, ctx: OperationContext
    ) -> ToolOutcome[ConvertCurrencyOutput]:
        request = ConversionRequest(
            source_evidence_id=args.evidence_id,
            columns=args.amount_columns,
            target_currency=args.target_currency,
            basis=args.rate_basis,
            as_of=args.as_of,
        )
        try:
            result = await service.convert(ctx, request)
        except ConversionRefused as refused:
            return ToolFailed(
                code=_ERROR_CODES[refused.reason], message=refused.message
            )
        table = result.evidence.content.table
        rate = result.rate
        return ToolSucceeded(
            output=ConvertCurrencyOutput(
                evidence_id=result.evidence.evidence_id,
                source_currency=result.source_currency,
                display_currency=rate.quote,
                rate=str(rate.rate),
                rate_date=rate.effective_date,
                rate_source=rate.source,
                rate_method=rate.method,
                rate_basis=result.basis.value,
                disclosure=result.disclosure,
                columns=table.column_names,
                converted_columns=result.converted_columns,
                rows=tuple(tuple(_cell(c) for c in row) for row in table.rows),
                truncated=table.truncated,
            ),
            empty=not table.rows,
        )

    return CapabilitySpec(
        name=CONVERT_CURRENCY,
        version=1,
        description=(
            "Convert amount columns of recorded query evidence to another "
            "currency. Returns converted and original amounts with the rate, "
            "its date and source. Refuses when the dataset currency is "
            "unverified or no rate exists; never estimate a conversion. Ask the "
            "user for current versus historical rates when it changes the numbers."
        ),
        progress_label="Converting amounts to the requested currency.",
        input_model=ConvertCurrencyInput,
        output_model=ConvertCurrencyOutput,
        handler=convert_currency,
        authorization=AuthorizationSpec(
            required_permissions=frozenset({Permission.ANALYSIS_READ.value}),
            requires_product_scope=True,
        ),
        # Records evidence keyed by the operation; a re-run fetches a fresh rate,
        # which must be a new operation rather than an automatic retry.
        side_effect=SideEffect.IDEMPOTENT_WRITE,
        retry=RetrySpec(RecoveryMode.NO_RETRY, 1, timedelta(seconds=30)),
    )
