from __future__ import annotations

from dataclasses import fields
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from retail_analytics.domain import metrics as m
from retail_analytics.domain.currency import SourceCurrency
from retail_analytics.domain.metric_evaluation import (
    MetricResult,
    Outcome,
    SalesItem,
    describe_basis,
    evaluate_metric,
)
from retail_analytics.domain.metric_preferences import (
    DefinitionPreference,
    resolve_term,
)
from retail_analytics.domain.periods import (
    DateWindow,
    OverrideScope,
    PeriodOrigin,
    as_of_date,
    default_period,
    equivalent_prior_period,
    month_window,
    previous_full_month,
    resolve_period,
)

CATALOG = m.default_catalog()
SEPT = month_window(2026, 9)


def item(
    n: int,
    status: str | None = "Complete",
    amount: str | None = "10.00",
    day: date = date(2026, 9, 10),
    order: str | None = None,
    customer: str = "c1",
) -> SalesItem:
    return SalesItem(
        item_ref=f"i{n}",
        order_ref=order or f"o{n}",
        customer_ref=customer,
        product_id="p1",
        item_status=status,
        ordered_date=day,
        sale_amount=None if amount is None else Decimal(amount),
    )


def run(metric: str, items: list[SalesItem], window: DateWindow = SEPT) -> MetricResult:
    return evaluate_metric(CATALOG, CATALOG.get(metric), items, window)


# --- catalog and definitions -------------------------------------------------


def test_catalog_has_contract_metrics_and_revenue_default() -> None:
    assert CATALOG.metric_ids() == {
        m.COMPLETED_ITEM_SALES,
        m.COMPLETED_ITEMS,
        m.COMPLETED_ORDERS,
        m.PURCHASING_CUSTOMERS,
        m.AVERAGE_ORDER_SALES,
        m.SALES_PER_CUSTOMER,
    }
    assert CATALOG.default_revenue().metric_id == m.COMPLETED_ITEM_SALES
    assert CATALOG.default_revenue().population.qualifying_statuses == {"Complete"}


def test_definitions_carry_no_access_or_identity_fields() -> None:
    names = {f.name for f in fields(m.MetricDefinition)}
    assert not names & {"product_ids", "product_scope", "executive_id", "entitlement"}


def test_unknown_field_rejected() -> None:
    base = CATALOG.get(m.COMPLETED_ITEM_SALES)
    with pytest.raises(m.MetricDefinitionError, match="not in"):
        m.MetricDefinition(
            metric_id="bad",
            version=1,
            description="x",
            relation=base.relation,
            grain="item",
            operation=m.Operation.SUM,
            unit=m.Unit.CURRENCY_AMOUNT,
            time_field="ordered_date",
            population=base.population,
            measure_field="customer_email",
        )


def test_sum_without_measure_and_ratio_without_parts_rejected() -> None:
    base = CATALOG.get(m.COMPLETED_ITEM_SALES)
    common = {
        "metric_id": "bad",
        "version": 1,
        "description": "x",
        "relation": base.relation,
        "grain": "item",
        "unit": m.Unit.COUNT,
        "time_field": "ordered_date",
        "population": base.population,
    }
    with pytest.raises(m.MetricDefinitionError):
        m.MetricDefinition(operation=m.Operation.SUM, **common)  # type: ignore[arg-type]
    with pytest.raises(m.MetricDefinitionError):
        m.MetricDefinition(operation=m.Operation.RATIO, **common)  # type: ignore[arg-type]


def test_catalog_rejects_exploratory_duplicates_and_dangling_ratios() -> None:
    base = CATALOG.get(m.COMPLETED_ITEM_SALES)
    explo = m.exploratory_definition(base, metric_id="x", rationale="r")
    with pytest.raises(m.MetricDefinitionError, match="review"):
        m.MetricCatalog((explo,))
    with pytest.raises(m.MetricDefinitionError, match="duplicate"):
        m.MetricCatalog((base, base))
    with pytest.raises(m.MetricDefinitionError, match="unknown metric"):
        m.MetricCatalog((CATALOG.get(m.AVERAGE_ORDER_SALES),))


def test_churn_has_no_default_definition() -> None:
    with pytest.raises(m.UnknownMetricError, match="no default definition"):
        CATALOG.get("churn")


# --- metric semantics --------------------------------------------------------


def test_revenue_matches_qualifying_completed_items_only() -> None:
    items = [
        item(1, amount="10.50"),
        item(2, amount="20.25"),
        item(3, status="Returned", amount="99"),
        item(4, status="Cancelled", amount="99"),
        item(5, status="Shipped", amount="99"),
        item(6, status=None, amount="99"),
        item(7, status="complete", amount="99"),  # unknown spelling never qualifies
        item(8, day=date(2026, 8, 31), amount="99"),  # before window
        item(9, day=date(2026, 10, 1), amount="99"),  # end bound is exclusive
    ]
    r = run(m.COMPLETED_ITEM_SALES, items)
    assert r.value == Decimal("30.75")
    assert r.qualifying_items == 2
    assert r.unrecognised_status_items == 5
    assert run(m.COMPLETED_ITEMS, items).value == 2


def test_window_boundaries_are_half_open() -> None:
    items = [item(1, day=date(2026, 9, 1)), item(2, day=date(2026, 9, 30))]
    assert run(m.COMPLETED_ITEMS, items).value == 2
    assert run(m.COMPLETED_ITEMS, [item(3, day=date(2026, 10, 1))]).value == 0


def test_distinct_counts_not_repeated_rows() -> None:
    items = [
        item(1, order="o1", customer="c1"),
        item(2, order="o1", customer="c1"),
        item(3, order="o2", customer="c2"),
    ]
    assert run(m.COMPLETED_ORDERS, items).value == 2
    assert run(m.PURCHASING_CUSTOMERS, items).value == 2
    assert run(m.AVERAGE_ORDER_SALES, items).value == Decimal(15)
    assert run(m.SALES_PER_CUSTOMER, items).value == Decimal(15)


def test_returned_orders_leave_the_qualifying_population() -> None:
    items = [item(1, order="o1"), item(2, order="o1", status="Returned")]
    assert run(m.COMPLETED_ORDERS, items).value == 1
    assert run(m.COMPLETED_ORDERS, [item(2, status="Returned")]).value == 0


def test_empty_population_counts_and_sums_are_zero_averages_are_null() -> None:
    for metric in (m.COMPLETED_ITEM_SALES, m.COMPLETED_ITEMS, m.COMPLETED_ORDERS):
        r = run(metric, [])
        assert r.value == 0
        assert r.outcome is Outcome.EMPTY_POPULATION
    r = run(m.AVERAGE_ORDER_SALES, [item(1, status="Returned")])
    assert r.value is None
    assert r.outcome is Outcome.UNDEFINED_ZERO_DENOMINATOR
    assert "denominator" in r.explanation


def test_null_amounts_are_not_zero_and_are_disclosed() -> None:
    r = run(m.COMPLETED_ITEM_SALES, [item(1, amount="5"), item(2, amount=None)])
    assert r.value == Decimal(5)
    assert r.missing_amount_items == 1
    assert any("no sale amount" in w for w in r.warnings())
    assert run(m.COMPLETED_ITEMS, [item(1, amount=None)]).value == 1


def test_all_amounts_missing_is_undefined_not_zero() -> None:
    items = [item(1, amount=None)]
    r = run(m.COMPLETED_ITEM_SALES, items)
    assert r.value is None
    assert r.outcome is Outcome.UNDEFINED_ALL_AMOUNTS_MISSING
    ratio = run(m.AVERAGE_ORDER_SALES, items)
    assert ratio.value is None
    assert ratio.outcome is Outcome.UNDEFINED_ALL_AMOUNTS_MISSING


def test_precision_preserved() -> None:
    items = [item(1, amount="0.1"), item(2, amount="0.2")]
    assert run(m.COMPLETED_ITEM_SALES, items).value == Decimal("0.3")


# --- currency ----------------------------------------------------------------


def test_currency_unknown_by_default_and_never_inferred() -> None:
    r = run(m.COMPLETED_ITEM_SALES, [item(1, amount="19.99")])
    assert r.currency == SourceCurrency.unknown()
    assert not r.currency.is_known
    assert any("currency is not verified" in w for w in r.warnings())
    assert "$" not in describe_basis(CATALOG.default_revenue(), SEPT, r.currency)
    assert "currency not verified" in describe_basis(
        CATALOG.default_revenue(), SEPT, r.currency
    )
    # Counts are unaffected by currency.
    assert not any("currency" in w for w in run(m.COMPLETED_ITEMS, []).warnings())


def test_known_currency_is_carried_and_validated() -> None:
    cur = SourceCurrency("USD")
    r = evaluate_metric(CATALOG, CATALOG.default_revenue(), [item(1)], SEPT, cur)
    assert r.currency.label() == "USD"
    assert not any("currency" in w for w in r.warnings())
    with pytest.raises(ValueError):
        SourceCurrency("usd")


def test_basis_disclosure_names_status_dates_and_scope() -> None:
    text = describe_basis(CATALOG.default_revenue(), SEPT, SourceCurrency.unknown())
    assert "status Complete" in text
    assert "2026-09-01 to 2026-09-30 inclusive" in text
    assert "permitted products" in text


# --- periods -----------------------------------------------------------------


def test_previous_full_month_defaults_and_year_boundary() -> None:
    assert previous_full_month(date(2026, 10, 8)) == DateWindow(
        date(2026, 9, 1), date(2026, 10, 1)
    )
    assert previous_full_month(date(2026, 1, 1)) == DateWindow(
        date(2025, 12, 1), date(2026, 1, 1)
    )
    assert previous_full_month(date(2024, 3, 31)) == DateWindow(
        date(2024, 2, 1), date(2024, 3, 1)
    )
    assert default_period(date(2026, 10, 8)).is_complete


def test_as_of_date_uses_utc_and_requires_aware_time() -> None:
    late = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
    assert as_of_date(late) == date(2026, 9, 30)
    with pytest.raises(ValueError):
        as_of_date(datetime(2026, 9, 30))


def test_partial_month_is_labelled_with_actual_bounds() -> None:
    p = resolve_period(month_window(2026, 10), date(2026, 10, 8))
    assert not p.is_complete
    assert p.effective == DateWindow(date(2026, 10, 1), date(2026, 10, 8))
    assert p.elapsed_days == 7
    label = p.label()
    assert "INCOMPLETE" in label
    assert "2026-10-01 to 2026-10-07 inclusive" in label
    assert "7 of 31 days" in label


def test_first_day_of_month_has_nothing_observed_yet() -> None:
    p = resolve_period(month_window(2026, 10), date(2026, 10, 1))
    assert p.is_empty and not p.is_complete
    c = equivalent_prior_period(p)
    assert c.prior.is_empty and not c.like_for_like


def test_future_period_is_empty_not_shifted() -> None:
    p = resolve_period(month_window(2026, 11), date(2026, 10, 8))
    assert p.is_empty
    assert p.requested == month_window(2026, 11)


def test_equivalent_elapsed_comparison_for_partial_month() -> None:
    p = resolve_period(month_window(2026, 10), date(2026, 10, 8))
    c = equivalent_prior_period(p)
    assert c.prior == DateWindow(date(2026, 9, 1), date(2026, 9, 8))
    assert c.prior_requested == month_window(2026, 9)
    assert c.like_for_like


def test_complete_month_compares_with_whole_prior_month() -> None:
    c = equivalent_prior_period(resolve_period(SEPT, date(2026, 10, 8)))
    assert c.prior == month_window(2026, 8)
    assert not c.like_for_like  # 31 days against 30
    assert "not like-for-like" in c.note
    jul = equivalent_prior_period(
        resolve_period(month_window(2026, 8), date(2026, 10, 8))
    )
    assert jul.prior == month_window(2026, 7) and jul.like_for_like


def test_comparison_flags_shorter_prior_month() -> None:
    p = resolve_period(month_window(2026, 3), date(2026, 3, 31))
    assert p.elapsed_days == 30
    c = equivalent_prior_period(p)
    assert c.prior == month_window(2026, 2)
    assert not c.like_for_like
    assert "not like-for-like" in c.note
    full = equivalent_prior_period(
        resolve_period(month_window(2026, 3), date(2026, 5, 1))
    )
    assert full.prior == month_window(2026, 2) and not full.like_for_like


def test_non_month_window_compares_with_preceding_equal_span() -> None:
    w = DateWindow(date(2026, 9, 10), date(2026, 9, 20))
    c = equivalent_prior_period(resolve_period(w, date(2026, 10, 8)))
    assert c.prior == DateWindow(date(2026, 8, 31), date(2026, 9, 10))
    partial = equivalent_prior_period(resolve_period(w, date(2026, 9, 15)))
    assert partial.prior == DateWindow(date(2026, 8, 31), date(2026, 9, 5))


def test_user_override_keeps_its_window_and_scope() -> None:
    w = DateWindow(date(2026, 9, 15), date(2026, 10, 15))
    p = resolve_period(
        w,
        date(2026, 10, 8),
        origin=PeriodOrigin.USER_OVERRIDE,
        override_scope=OverrideScope.REPORT,
    )
    assert p.requested == w
    assert p.effective == DateWindow(date(2026, 9, 15), date(2026, 10, 8))
    assert p.override_scope is OverrideScope.REPORT
    assert not p.is_complete
    with pytest.raises(ValueError):
        resolve_period(w, date(2026, 10, 8), origin=PeriodOrigin.USER_OVERRIDE)
    with pytest.raises(ValueError):
        resolve_period(w, date(2026, 10, 8), override_scope=OverrideScope.SESSION)


def test_metric_over_partial_period_uses_effective_window() -> None:
    p = resolve_period(month_window(2026, 10), date(2026, 10, 8))
    items = [item(1, day=date(2026, 10, 7)), item(2, day=date(2026, 10, 8))]
    assert run(m.COMPLETED_ITEMS, items, p.effective).value == 1


# --- preferences, exploration, churn ----------------------------------------


def test_revenue_defaults_to_completed_item_sales_without_clarification() -> None:
    r = resolve_term(CATALOG, "revenue")
    assert r.definition.metric_id == m.COMPLETED_ITEM_SALES
    assert r.source == "shared default"


def test_preference_scope_precedence_and_shared_metric_untouched() -> None:
    items = m.COMPLETED_ITEMS
    prefs = (
        DefinitionPreference("revenue", items, 1, OverrideScope.USER_DEFAULT, "ask 1"),
        DefinitionPreference(
            "revenue", m.COMPLETED_ORDERS, 1, OverrideScope.REPORT, "ask 2"
        ),
    )
    assert (
        resolve_term(CATALOG, "revenue", prefs).definition.metric_id
        == m.COMPLETED_ORDERS
    )
    assert resolve_term(CATALOG, "revenue", prefs[:1]).definition.metric_id == items
    # Other terms and the shared default are unaffected.
    assert CATALOG.default_revenue().metric_id == m.COMPLETED_ITEM_SALES
    with pytest.raises(m.UnknownMetricError):
        resolve_term(
            CATALOG,
            "revenue",
            (DefinitionPreference("revenue", "nope", 1, OverrideScope.SESSION, "x"),),
        )


def test_exploratory_calculation_is_possible_and_labelled() -> None:
    base = CATALOG.get(m.COMPLETED_ITEM_SALES)
    explo = m.exploratory_definition(
        base,
        metric_id="sales_incl_shipped",
        rationale="User asked to include shipped items",
        qualifying_statuses={"Complete", "Shipped"},
    )
    items = [item(1, amount="10"), item(2, status="Shipped", amount="5")]
    r = evaluate_metric(CATALOG, explo, items, SEPT)
    assert r.value == Decimal(15)
    assert r.is_exploratory
    assert any("Exploratory" in w for w in r.warnings())
    assert "Exploratory definition" in describe_basis(explo, SEPT, r.currency)
    assert explo.metric_id not in CATALOG.metric_ids()
    with pytest.raises(m.MetricDefinitionError):
        m.exploratory_definition(base, metric_id="x", rationale="")


def test_exploratory_ratio_uses_its_own_population() -> None:
    avg = m.exploratory_definition(
        CATALOG.get(m.AVERAGE_ORDER_SALES),
        metric_id="avg_incl_shipped",
        rationale="r",
        qualifying_statuses={"Complete", "Shipped"},
    )
    items = [item(1, amount="10"), item(2, status="Shipped", amount="20")]
    assert evaluate_metric(CATALOG, avg, items, SEPT).value == Decimal(15)


def test_churn_requires_explicit_definition_and_is_never_company_wide() -> None:
    d = m.ChurnDefinition(
        population="customers with a completed purchase in the prior 12 months",
        inactivity_days=90,
        observation_days=365,
        rationale="Executive asked about lapsed buyers",
    )
    assert "permitted products" in d.describe()
    assert "not company-wide" in d.describe()
    with pytest.raises(m.MetricDefinitionError):
        m.ChurnDefinition("", 90, 365, "r")
    with pytest.raises(m.MetricDefinitionError):
        m.ChurnDefinition("p", 90, 30, "r")
