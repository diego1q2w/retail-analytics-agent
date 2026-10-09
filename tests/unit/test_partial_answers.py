"""The no-model fallback answer: relevant findings only, readable figures.

Reproduces a September-revenue request that stopped at its token budget
after earlier unrelated session results (an overview, yearly totals, a
monthly breakdown) and one result whose recorded period and definition
basis match the question.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from retail_analytics.application.contracts.investigations import StopReason
from retail_analytics.application.investigation_runtime import stop_message
from retail_analytics.application.partial_answers import (
    RequestFocus,
    format_value,
    render_partial,
    select_relevant,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.budgets import BudgetResource
from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.currency import SourceCurrency
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    AuthorityStamp,
    DefinitionRef,
    Evidence,
    EvidenceCell,
    EvidenceColumn,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    Provenance,
    ReuseBlock,
    TermMeaning,
    content_digest,
)
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.periods import DateWindow

RUN = "run-now"
EARLIER = "run-earlier"
NOW = datetime(2026, 10, 9, 10, tzinfo=UTC)
SCOPE = ProductScope(frozenset({"1"}), 2)
SALES = DefinitionRef("completed_item_sales", 1)
ITEMS = DefinitionRef("completed_items", 1)
REVENUE = frozenset({TermMeaning("revenue", SALES)})
SEPTEMBER = DateWindow(date(2026, 9, 1), date(2026, 10, 1))
YEAR = DateWindow(date(2026, 1, 1), date(2027, 1, 1))
METRICS = default_catalog()
UNKNOWN = SourceCurrency.unknown()
TOKENS_STOP = stop_message(StopReason.BUDGET, BudgetResource.TOKENS)
SALE = ("sales_items.sale_amount",)
STATUS = ("sales_items.item_status",)


def record(
    evidence_id: str,
    *,
    run_id: str = RUN,
    period: DateWindow | None,
    columns: tuple[EvidenceColumn, ...],
    rows: tuple[tuple[EvidenceCell, ...], ...],
    grain: tuple[str, ...] = (),
    minutes: int = 0,
    recorded: bool = True,
    truncation: str | None = None,
) -> Evidence:
    content = EvidenceContent(
        kind=EvidenceKind.QUERY,
        subject_key=f"q:{evidence_id}",
        analysis=AnalysisStamp(
            1,
            1,
            frozenset({SALES, ITEMS}) if recorded else frozenset(),
            "fp",
            period,
            terms=REVENUE if recorded else frozenset(),
            date_basis="ordered_date",
            definitions_recorded=recorded,
        ),
        provenance=Provenance(logical_sql="SELECT 1", executed_query_digest="d" * 64),
        table=EvidenceTable(columns, rows, len(rows), truncation),
        grain=grain,
    )
    computed = NOW + timedelta(minutes=minutes)
    return Evidence(
        evidence_id=evidence_id,
        lineage_id=evidence_id,
        version=1,
        executive_id="exec-a",
        session_id="ses-a",
        run_id=run_id,
        operation_id=f"op-{evidence_id}",
        authority=AuthorityStamp.of(SCOPE),
        content=content,
        computed_at=computed,
        content_digest=content_digest(content, computed),
    )


def usable(evidence: Evidence) -> EvidenceStanding:
    return EvidenceStanding(evidence, None)


YEAR_COLUMNS = (
    EvidenceColumn("order_year", "value", ("sales_items.ordered_date",)),
    EvidenceColumn("revenue", "value", SALE),
)
OVERVIEW = record(
    "evd_overview",
    run_id=EARLIER,
    period=None,
    columns=(
        EvidenceColumn("status", "value", STATUS),
        EvidenceColumn("orders", "value", ("sales_items.order_id",)),
    ),
    rows=(("Complete", 51234), ("Cancelled", 9876)),
    grain=("status",),
    minutes=-60,
)
EARLIER_YEARS = record(
    "evd_years_old",
    run_id=EARLIER,
    period=None,
    columns=YEAR_COLUMNS,
    rows=((2025, 1234567.891234), (2026, 987654.3219876)),
    grain=("order_year",),
    minutes=-50,
)
SEPTEMBER_BY_YEAR = record(
    "evd_sept_years",
    period=None,
    columns=YEAR_COLUMNS,
    rows=((2024, 120001.123456), (2025, 130002.654321), (2026, 141190.75015928224)),
    grain=("order_year",),
    minutes=1,
)
SEPTEMBER_BY_STATUS = record(
    "evd_sept_2026",
    period=SEPTEMBER,
    columns=(
        EvidenceColumn("status", "value", STATUS),
        EvidenceColumn("revenue", "value", SALE),
    ),
    rows=(("Complete", 141190.75015928224),),
    grain=("status",),
    minutes=2,
)
MONTHLY_2026 = record(
    "evd_monthly_2026",
    period=YEAR,
    columns=(
        EvidenceColumn("order_month", "value", ("sales_items.ordered_date",)),
        EvidenceColumn("revenue", "value", SALE),
    ),
    rows=tuple((f"2026-{m:02d}", 10000.5 + m) for m in range(1, 10)),
    grain=("order_month",),
    minutes=3,
)
SESSION = [
    usable(e)
    for e in (
        OVERVIEW,
        EARLIER_YEARS,
        SEPTEMBER_BY_YEAR,
        SEPTEMBER_BY_STATUS,
        MONTHLY_2026,
    )
]
QUESTION = "what's the latest revienew of september?"


def test_request_focus_reads_months_years_and_words_only() -> None:
    focus = RequestFocus.of("Revenue for Sept 2026 vs august?")
    assert focus.months == {9, 8}
    assert focus.years == {2026}
    assert "revenue" in focus.words


def test_september_revenue_token_stop_shows_only_the_matching_result() -> None:
    selection = select_relevant(QUESTION, SESSION, run_id=RUN)
    assert [s.evidence.evidence_id for s in selection.relevant] == ["evd_sept_2026"]
    assert selection.others == 4

    text, cited = render_partial(
        TOKENS_STOP, selection, metrics=METRICS, currency=UNKNOWN
    )
    assert cited == ("evd_sept_2026",)
    assert text.startswith(TOKENS_STOP)
    assert "model token budget" in text
    assert "2026-09-01 to 2026-09-30 inclusive (UTC)" in text
    assert "definition basis completed item sales v1" in text
    assert "Complete: revenue 141,190.75" in text
    assert "currency, which is not verified" in text
    assert "Not answered" in text
    assert "4 other results" in text
    # No unrelated tables, no unrounded floats, IDs only in the citation line.
    for unrelated in ("evd_overview", "evd_years_old", "evd_sept_years"):
        assert unrelated not in text
    assert "evd_monthly_2026" not in text
    assert "51,234" not in text and "1,234,567" not in text
    assert not re.search(r"\d+\.\d{3,}", text)
    assert text.count("evd_sept_2026") == 1
    assert text.rstrip().endswith("Evidence: evd_sept_2026")
    # A token stop is not a truncated result.
    assert "cut off" not in text


def test_misaligned_period_or_measure_gives_no_figure() -> None:
    unrelated = [usable(e) for e in (OVERVIEW, EARLIER_YEARS, SEPTEMBER_BY_YEAR)]
    selection = select_relevant(QUESTION, unrelated, run_id=RUN)
    assert selection.relevant == ()
    text, cited = render_partial(
        TOKENS_STOP, selection, metrics=METRICS, currency=UNKNOWN
    )
    assert cited == ()
    assert "No verified result matches the period and measure" in text
    assert "no figure is given" in text
    assert "3 other results" in text
    # No number from any stored row is presented.
    assert not re.search(r"\d{3,}", text.replace(TOKENS_STOP, ""))


def test_records_without_a_recorded_definition_basis_do_not_match() -> None:
    legacy = record(
        "evd_legacy",
        period=SEPTEMBER,
        columns=YEAR_COLUMNS,
        rows=((2026, 5.0),),
        recorded=False,
    )
    selection = select_relevant("September revenue", [usable(legacy)], run_id=RUN)
    assert selection.relevant == ()


def test_no_evidence_says_nothing_was_verified() -> None:
    selection = select_relevant(QUESTION, [], run_id=RUN)
    text, cited = render_partial(
        TOKENS_STOP, selection, metrics=METRICS, currency=UNKNOWN
    )
    assert cited == ()
    assert "No verified results were produced yet." in text
    assert "Your question was not answered." in text
    assert "other result" not in text


def test_revoked_or_invalidated_records_are_never_selected() -> None:
    revoked = EvidenceStanding(SEPTEMBER_BY_STATUS, ReuseBlock.AUTHORIZATION_CHANGED)
    selection = select_relevant(QUESTION, [revoked], run_id=RUN)
    assert selection == type(selection)((), 0)


def test_prior_session_result_is_reused_when_it_matches() -> None:
    earlier = record(
        "evd_sept_prior",
        run_id=EARLIER,
        period=SEPTEMBER,
        columns=(EvidenceColumn("revenue", "value", SALE),),
        rows=((141190.75015928224,),),
        minutes=-30,
    )
    selection = select_relevant(
        QUESTION, [usable(earlier), usable(OVERVIEW)], run_id=RUN
    )
    assert [s.evidence.evidence_id for s in selection.relevant] == ["evd_sept_prior"]
    text, _ = render_partial(
        TOKENS_STOP,
        selection,
        metrics=METRICS,
        currency=SourceCurrency.declared_by_operator("USD"),
    )
    assert "revenue 141,190.75 USD" in text
    assert "declared by operator, not verified" in text


def test_without_period_or_measure_only_this_runs_results_count() -> None:
    selection = select_relevant("give me an overview", SESSION, run_id=RUN)
    assert {s.evidence.run_id for s in selection.relevant} == {RUN}
    assert len(selection.relevant) == 3


def test_truncated_relevant_result_is_labelled_as_cut_off() -> None:
    cut = record(
        "evd_cut",
        period=SEPTEMBER,
        columns=(EvidenceColumn("revenue", "value", SALE),),
        rows=((1.0,),),
        truncation="row_limit",
    )
    text, _ = render_partial(
        TOKENS_STOP,
        select_relevant(QUESTION, [usable(cut)], run_id=RUN),
        metrics=METRICS,
        currency=UNKNOWN,
    )
    assert "The result was cut off: rows are missing from it." in text


def test_without_rows_the_text_has_no_figures() -> None:
    selection = select_relevant(QUESTION, SESSION, run_id=RUN)
    text, cited = render_partial(
        TOKENS_STOP, selection, metrics=METRICS, currency=UNKNOWN, rows=False
    )
    assert cited == ("evd_sept_2026",)
    assert "141" not in text
    assert "figures not shown" in text


VALUE = EvidenceColumn("total", "value")


@pytest.mark.parametrize(
    ("cell", "column", "money", "expected"),
    [
        (141190.75015928224, VALUE, True, "141,190.75"),
        (Decimal("1234.5"), VALUE, False, "1,234.50"),
        (51234, VALUE, False, "51,234"),
        (3.0, VALUE, False, "3"),
        (0.012345, VALUE, False, "0.0123"),
        (12.3456, EvidenceColumn("margin_pct", "value"), False, "12.3%"),
        (0.4567, EvidenceColumn("repeat_rate", "value"), False, "45.7%"),
        (None, VALUE, False, "-"),
        ("Complete", VALUE, False, "Complete"),
        (date(2026, 9, 1), VALUE, False, "2026-09-01"),
        (12345, EvidenceColumn("product_ref", "reference"), False, "12345"),
    ],
)
def test_values_are_formatted_for_reading(
    cell: EvidenceCell, column: EvidenceColumn, money: bool, expected: str
) -> None:
    assert format_value(cell, column, money=money, currency=UNKNOWN) == expected
