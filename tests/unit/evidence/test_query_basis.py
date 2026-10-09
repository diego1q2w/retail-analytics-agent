"""Definition basis recorded on query evidence from trusted data (T18-F3)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from retail_analytics.application.evidence import query_basis
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    DefinitionRef,
    TermMeaning,
    decode_analysis,
    encode_analysis,
)
from retail_analytics.domain.metrics import (
    COMPLETE_STATUS,
    COMPLETED_ITEM_SALES,
    COMPLETED_ITEMS,
    MetricCatalog,
    PopulationFilter,
    builtin_metrics,
    default_catalog,
)
from retail_analytics.domain.periods import DateWindow, OverrideScope
from retail_analytics.domain.preferences import (
    EffectiveEntry,
    EffectivePreferences,
    PreferenceSetting,
)
from tests.unit.sql_compiler.support import compile_sql

SEPTEMBER = DateWindow(date(2026, 9, 1), date(2026, 10, 1))
REVENUE_SQL = (
    "SELECT s.product_id, SUM(s.sale_amount) AS revenue FROM sales_items AS s "
    "WHERE s.item_status = 'Complete' AND s.ordered_date >= DATE '2026-09-01' "
    "AND s.ordered_date < DATE '2026-10-01' GROUP BY s.product_id"
)
NO_PREFERENCES = EffectivePreferences(())


def test_revenue_query_records_definitions_terms_period_and_basis() -> None:
    basis = query_basis(
        compile_sql(REVENUE_SQL), metrics=default_catalog(), effective=NO_PREFERENCES
    )

    assert basis.definitions_recorded
    assert DefinitionRef(COMPLETED_ITEM_SALES, 1) in basis.definitions
    assert DefinitionRef(COMPLETED_ITEMS, 1) in basis.definitions
    assert basis.terms == frozenset(
        {TermMeaning("revenue", DefinitionRef(COMPLETED_ITEM_SALES, 1))}
    )
    assert basis.period == SEPTEMBER
    assert basis.date_basis == "ordered_date"
    assert basis.time_zone == "UTC"
    assert basis.preference_fingerprint == NO_PREFERENCES.analytical_fingerprint
    assert basis.analytical_slots == frozenset({"metric_definition:revenue"})


def test_query_without_metric_fields_records_an_empty_known_basis() -> None:
    basis = query_basis(
        compile_sql(
            "SELECT p.category, COUNT(*) AS n FROM products AS p GROUP BY p.category"
        ),
        metrics=default_catalog(),
        effective=NO_PREFERENCES,
    )
    assert basis.definitions_recorded
    assert basis.definitions == frozenset() and basis.terms == frozenset()
    assert basis.period is None and basis.date_basis is None


def test_preferred_meaning_of_revenue_is_recorded() -> None:
    base = default_catalog().get(COMPLETED_ITEM_SALES)
    shipped = replace(
        base,
        metric_id="shipped_item_sales",
        population=PopulationFilter(
            "item_status", frozenset({COMPLETE_STATUS, "Shipped"})
        ),
    )
    catalog = MetricCatalog((*builtin_metrics(), shipped))
    effective = EffectivePreferences(
        (
            EffectiveEntry(
                PreferenceSetting.metric("revenue", "shipped_item_sales", 1),
                OverrideScope.USER_DEFAULT,
                "explicit v1",
            ),
        )
    )

    basis = query_basis(compile_sql(REVENUE_SQL), metrics=catalog, effective=effective)

    assert basis.terms == frozenset(
        {TermMeaning("revenue", DefinitionRef("shipped_item_sales", 1))}
    )
    assert DefinitionRef("shipped_item_sales", 1) in basis.definitions
    assert basis.preference_fingerprint == effective.analytical_fingerprint


def test_unrecorded_stamps_keep_their_original_encoding() -> None:
    legacy = AnalysisStamp(1, 1, frozenset(), "fp", SEPTEMBER)
    encoded = encode_analysis(legacy)
    # Exactly the keys stored before T18-F3, so old content digests still hold.
    assert set(encoded) == {
        "catalog_version",
        "policy_version",
        "definitions",
        "preference_fingerprint",
        "period",
        "time_zone",
    }
    assert not decode_analysis(encoded).definitions_recorded

    recorded = replace(
        legacy,
        definitions=frozenset({DefinitionRef(COMPLETED_ITEM_SALES, 1)}),
        terms=frozenset(
            {TermMeaning("revenue", DefinitionRef(COMPLETED_ITEM_SALES, 1))}
        ),
        date_basis="ordered_date",
        definitions_recorded=True,
    )
    assert decode_analysis(encode_analysis(recorded)) == recorded
