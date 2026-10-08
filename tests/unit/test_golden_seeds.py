"""The Golden seed library: shape, SQL, figures, sanitization and approval.

Every figure a seed report quotes is a declared ``Claim``. Each claim is
recomputed two ways that share nothing but the fixture rows: plain Python over
the rows (with the reference metric semantics where a metric exists) and
the seed's own SQL, compiled by the restricted compiler and run on DuckDB.
Both must equal the declared value.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.application.artifacts import (
    ArtifactMaintenance,
    ArtifactPolicy,
    ArtifactService,
)
from retail_analytics.application.authorization import (
    AccessResolver,
    OwnershipGuard,
    Principal,
)
from retail_analytics.application.golden_seed_library import (
    SEED_SCHEMA_VERSION,
    Claim,
    SeedExample,
    Step,
    check_library,
    seed_library,
)
from retail_analytics.application.golden_seeding import (
    SeedLibraryInvalid,
    SeedOutcome,
    seed_golden_library,
    seed_principals,
)
from retail_analytics.application.knowledge import (
    GoldenKnowledgeReader,
    KnowledgeError,
    KnowledgeErrorCode,
    KnowledgeService,
)
from retail_analytics.application.query_compiler import ScalarValue
from retail_analytics.domain.access import ExecutiveAccess, ProductScope, Role
from retail_analytics.domain.knowledge import (
    ApplicabilityContext,
    ExampleRef,
    Origin,
    ReviewAction,
    ReviewStatus,
    SourceKind,
)
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.metric_evaluation import SalesItem, evaluate_metric
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.periods import DateWindow, month_window
from retail_analytics.domain.sensitive_content import screen_fields
from tests.golden_seed_fixture import CUSTOMERS, PRODUCTS, SCOPE, database, sales_items
from tests.unit.memory_knowledge import MemoryKnowledgeRepository
from tests.unit.sql_compiler.support import COMPILER, compile_sql, duckdb_sql
from tests.unit.test_artifacts import MemoryCatalog

LIBRARY = seed_library()
CATALOG = default_catalog()
ITEMS = sales_items()
JUL_SEP = DateWindow(date(2026, 7, 1), date(2026, 10, 1))
NOW = datetime(2026, 10, 8, tzinfo=UTC)
CENTS = Decimal("0.01")
TENTH = Decimal("0.1")

type Row = dict[str, Any]


# -- shape ---------------------------------------------------------------------


def test_library_is_eight_to_twelve_distinct_examples() -> None:
    assert 8 <= len(LIBRARY) <= 12
    assert len({e.key for e in LIBRARY}) == len(LIBRARY)
    assert len({e.example_id for e in LIBRARY}) == len(LIBRARY)
    assert len({e.question for e in LIBRARY}) == len(LIBRARY)


def test_library_covers_the_required_themes() -> None:
    themes = {t for e in LIBRARY for t in e.themes}
    required = {
        "trends",
        "products",
        "customers",
        "demographics",
        "multi-step",
        "empty results",
        "causal limits",
    }
    assert required <= themes


def test_library_passes_its_own_structural_checks() -> None:
    assert check_library(LIBRARY, default_logical_catalog(), CATALOG) == []


def test_structural_checks_catch_bad_content() -> None:
    from dataclasses import replace

    base = LIBRARY[0]
    bad = replace(
        base,
        report_body=base.report_body + "\nRevenue was 999 last week.\n",
        claims=(*base.claims, Claim("phantom", "4321.5", 1)),
    )
    problems = check_library([bad, *LIBRARY[1:]], default_logical_catalog(), CATALOG)
    assert any("999" in p for p in problems)
    assert any("is not in the report" in p for p in problems)
    few = check_library(LIBRARY[:3], default_logical_catalog(), CATALOG)
    assert any("expected 8-12" in p for p in few)
    unknown = replace(
        base, metrics=frozenset({type(next(iter(base.metrics)))("nope", 1)})
    )
    assert any(
        "unknown metric" in p
        for p in check_library(
            [unknown, *LIBRARY[1:]], default_logical_catalog(), CATALOG
        )
    )


@pytest.mark.parametrize("example", LIBRARY, ids=lambda e: e.key)
def test_example_has_every_part_and_honest_labels(example: SeedExample) -> None:
    assert example.question.strip() and example.sql.strip()
    assert example.method_summary.strip() and example.report_markdown.strip()
    assert example.metrics, "an example must reference metric ids and versions"
    assert example.report_markdown.startswith(f"# {example.title}")
    assert "Illustrative worked example" in example.report_markdown
    assert "synthetic reference fixture" in example.report_markdown
    assert "## Assumptions and limits" in example.report_markdown
    assert "## Definition and scope" in example.report_markdown
    assert "UTC" in example.report_markdown


@pytest.mark.parametrize("example", LIBRARY, ids=lambda e: e.key)
def test_metric_and_schema_versions_are_current_and_compatible(
    example: SeedExample,
) -> None:
    logical = default_logical_catalog()
    assert f"logical-catalog/{logical.version}" == SEED_SCHEMA_VERSION
    for ref in example.metrics:
        assert not CATALOG.get(ref.metric_id, ref.version).is_exploratory
    context = ApplicabilityContext(
        SEED_SCHEMA_VERSION,
        {m: CATALOG.latest_version(m) for m in CATALOG.metric_ids()},
    )
    from retail_analytics.application.golden_seeding import draft_for

    assert draft_for(example).applicability.applies_to(context)
    stale = ApplicabilityContext("logical-catalog/999", dict(context.metric_versions))
    assert not draft_for(example).applicability.applies_to(stale)


def test_revenue_follows_the_catalog_definition() -> None:
    revenue = CATALOG.default_revenue()
    assert revenue.metric_id == "completed_item_sales"
    status_filter = r"item_status = 'Complete'"
    for example in LIBRARY:
        for step in example.steps:
            sql = step.sql
            if (
                "SUM(" in sql
                and "sale_amount" in sql
                and not re.search(r"GROUP BY[^;]*item_status", sql)
            ):
                assert status_filter in sql, (example.key, step.title)
            for name in re.findall(r"@(\w+)", sql):
                assert name in step.parameters, (example.key, name)
            if "ordered_date" in sql:
                # windows are half-open: inclusive start, exclusive end
                assert "BETWEEN" not in sql.upper()
                assert re.search(r"ordered_date >= @\w+", sql)
                assert re.search(r"ordered_date < @\w+", sql)
            assert "<=" not in sql


# -- SQL validates and runs ----------------------------------------------------


@pytest.fixture(scope="module")
def db() -> Iterable[duckdb.DuckDBPyConnection]:
    connection = database()
    yield connection
    connection.close()


def run(
    db: duckdb.DuckDBPyConnection, sql: str, parameters: Mapping[str, ScalarValue]
) -> list[Row]:
    compiled = compile_sql(sql, SCOPE, parameters)
    statement = duckdb_sql(compiled)
    values = {
        p.name: list(p.value) if isinstance(p.value, tuple) else p.value
        for p in compiled.parameters
        if f"${p.name}" in statement
    }
    cursor = db.execute(statement, values)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def run_step(db: duckdb.DuckDBPyConnection, step: Step) -> list[Row]:
    return run(db, step.sql, step.parameters)


def steps_of(example: SeedExample) -> list[Step]:
    return list(example.steps)


@pytest.mark.parametrize("example", LIBRARY, ids=lambda e: e.key)
def test_every_step_compiles_and_the_stored_sql_is_those_steps(
    example: SeedExample,
) -> None:
    for step in example.steps:
        compiled = compile_sql(step.sql, SCOPE, step.parameters)
        assert compiled.catalog_version == default_logical_catalog().version
        assert (
            "sales_items" in compiled.logical_sql or "products" in compiled.logical_sql
        )
    stored = [part.strip() for part in example.sql.split(";") if part.strip()]
    assert len(stored) == len(example.steps)
    for part, step in zip(stored, example.steps, strict=True):
        assert step.sql.strip() in part


def test_the_compiler_rejects_the_whole_script_but_accepts_each_step() -> None:
    from retail_analytics.application.query_compiler import AnalysisQuery, QueryRejected
    from tests.unit.sql_compiler.support import view

    example = next(e for e in LIBRARY if len(e.steps) > 1)
    with pytest.raises(QueryRejected):
        COMPILER.compile(AnalysisQuery(example.sql, {}), catalog=view(), scope=SCOPE)


@pytest.mark.parametrize("example", LIBRARY, ids=lambda e: e.key)
def test_every_step_runs_on_the_fixture(
    example: SeedExample, db: duckdb.DuckDBPyConnection
) -> None:
    for step in example.steps:
        run_step(db, step)


# -- figures: two independent routes -------------------------------------------


def money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def pct(part: Any, whole: Any) -> str:
    ratio = Decimal(str(part)) * 100 / Decimal(str(whole))
    return str(ratio.quantize(TENTH, rounding=ROUND_HALF_UP))


def select(
    start: date, end: date, *, status: str | None = "Complete"
) -> list[SalesItem]:
    return [
        i
        for i in ITEMS
        if start <= i.ordered_date < end and (status is None or i.item_status == status)
    ]


def total(items: Iterable[SalesItem]) -> Decimal:
    return sum((i.sale_amount or Decimal(0) for i in items), Decimal(0))


def orders(items: Iterable[SalesItem]) -> int:
    return len({i.order_ref for i in items})


def metric(metric_id: str, window: DateWindow) -> Decimal | int:
    result = evaluate_metric(CATALOG, CATALOG.get(metric_id), ITEMS, window)
    assert result.value is not None
    return result.value


PRODUCT = {str(p.product_id): p for p in PRODUCTS}
CUSTOMER = {f"customer-{c.customer_id}": c for c in CUSTOMERS}


def age_band(age: int) -> str:
    return "under 30" if age < 30 else "30-49" if age < 50 else "50+"


Claims = dict[str, Any]


def trend_reference() -> Claims:
    months = [month_window(2026, m) for m in (7, 8, 9)]
    sales = [metric("completed_item_sales", w) for w in months]
    counts = [metric("completed_orders", w) for w in months]
    return {
        "jul_sales": sales[0],
        "aug_sales": sales[1],
        "sep_sales": sales[2],
        "jul_orders": counts[0],
        "aug_orders": counts[1],
        "sep_orders": counts[2],
        "aug_change": sales[1] - sales[0],
        "aug_change_pct": pct(sales[1] - sales[0], sales[0]),
        "sep_change": sales[2] - sales[1],
        "sep_change_pct": pct(sales[2] - sales[1], sales[1]),
    }


def trend_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    rows = run_step(db, LIBRARY[0].steps[0])
    s = [money(r["completed_item_sales"]) for r in rows]
    n = [r["completed_orders"] for r in rows]
    assert [r["month"].month for r in rows] == [7, 8, 9]
    return {
        "jul_sales": s[0],
        "aug_sales": s[1],
        "sep_sales": s[2],
        "jul_orders": n[0],
        "aug_orders": n[1],
        "sep_orders": n[2],
        "aug_change": s[1] - s[0],
        "aug_change_pct": pct(s[1] - s[0], s[0]),
        "sep_change": s[2] - s[1],
        "sep_change_pct": pct(s[2] - s[1], s[1]),
    }


def partial_reference() -> Claims:
    current = metric(
        "completed_item_sales", DateWindow(date(2026, 10, 1), date(2026, 10, 8))
    )
    previous = metric(
        "completed_item_sales", DateWindow(date(2026, 9, 1), date(2026, 9, 8))
    )
    return {
        "current_sales": current,
        "previous_sales": previous,
        "change": current - previous,
        "change_pct": pct(current - previous, previous),
        "excluded_today": total(select(date(2026, 10, 8), date(2026, 10, 9))),
        "full_september": metric("completed_item_sales", month_window(2026, 9)),
    }


def partial_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    (row,) = run_step(db, LIBRARY[1].steps[0])
    current, previous = (
        money(row["current_period_sales"]),
        money(row["previous_comparable_sales"]),
    )
    adhoc = (
        "SELECT SUM(sale_amount) AS v FROM sales_items WHERE item_status = 'Complete' "
        "AND ordered_date >= @a AND ordered_date < @b"
    )
    (today,) = run(db, adhoc, {"a": date(2026, 10, 8), "b": date(2026, 10, 9)})
    (september,) = run(db, adhoc, {"a": date(2026, 9, 1), "b": date(2026, 10, 1)})
    return {
        "current_sales": current,
        "previous_sales": previous,
        "change": current - previous,
        "change_pct": pct(current - previous, previous),
        "excluded_today": money(today["v"]),
        "full_september": money(september["v"]),
    }


def top_products_reference() -> Claims:
    by_product: dict[str, list[SalesItem]] = defaultdict(list)
    for item in select(JUL_SEP.start, JUL_SEP.end):
        by_product[item.product_id].append(item)
    ranked = sorted(
        by_product.items(), key=lambda kv: (-total(kv[1]), PRODUCT[kv[0]].name)
    )[:5]
    claims: Claims = {}
    for index, (_, items) in enumerate(ranked, start=1):
        claims[f"p{index}_sales"] = total(items)
        claims[f"p{index}_items"] = len(items)
    all_total = metric("completed_item_sales", JUL_SEP)
    top5 = sum((total(items) for _, items in ranked), Decimal(0))
    claims.update(
        top5_total=top5,
        all_total=all_total,
        top5_share_pct=pct(top5, all_total),
        gap_top_two=total(ranked[0][1]) - total(ranked[1][1]),
    )
    return claims


def top_products_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    rows = run_step(db, LIBRARY[2].steps[0])
    (everything,) = run_step(db, LIBRARY[2].steps[1])
    claims: Claims = {}
    for index, row in enumerate(rows, start=1):
        claims[f"p{index}_sales"] = money(row["completed_item_sales"])
        claims[f"p{index}_items"] = row["completed_items"]
    top5 = sum((claims[f"p{i}_sales"] for i in range(1, 6)), Decimal(0))
    all_total = money(everything["completed_item_sales"])
    claims.update(
        top5_total=top5,
        all_total=all_total,
        top5_share_pct=pct(top5, all_total),
        gap_top_two=claims["p1_sales"] - claims["p2_sales"],
    )
    return claims


def brand_reference() -> Claims:
    claims: Claims = {}
    sold = select(JUL_SEP.start, JUL_SEP.end)
    brand_order_total = 0
    for brand in ("Gamma", "Alpha", "Delta", "Beta"):
        items = [i for i in sold if PRODUCT[i.product_id].brand == brand]
        claims[f"{brand.lower()}_sales"] = total(items)
        claims[f"{brand.lower()}_orders"] = orders(items)
        claims[f"{brand.lower()}_aos"] = money(total(items) / orders(items))
        brand_order_total += orders(items)
    claims.update(
        all_sales=metric("completed_item_sales", JUL_SEP),
        brand_orders_sum=brand_order_total,
        distinct_orders=metric("completed_orders", JUL_SEP),
        overall_aos=money(metric("average_order_sales", JUL_SEP)),
    )
    return claims


def brand_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    rows = {r["brand"]: r for r in run_step(db, LIBRARY[3].steps[0])}
    (overall,) = run_step(db, LIBRARY[3].steps[1])
    claims: Claims = {}
    for brand, row in rows.items():
        key = brand.lower()
        claims[f"{key}_sales"] = money(row["completed_item_sales"])
        claims[f"{key}_orders"] = row["completed_orders"]
        claims[f"{key}_aos"] = money(row["average_order_sales"])
    claims.update(
        all_sales=money(overall["completed_item_sales"]),
        brand_orders_sum=sum(r["completed_orders"] for r in rows.values()),
        distinct_orders=overall["completed_orders"],
        overall_aos=money(
            Decimal(str(overall["completed_item_sales"])) / overall["completed_orders"]
        ),
    )
    return claims


def unsold_reference() -> Claims:
    sold_ids = {i.product_id for i in select(JUL_SEP.start, JUL_SEP.end)}
    unsold = [p for p in PRODUCTS if str(p.product_id) not in sold_ids]
    return {
        "unsold_count": len(unsold),
        "unsold_name": unsold[0].name,
        "product_count": len(PRODUCTS),
        "sold_count": len(sold_ids),
    }


def unsold_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    rows = run_step(db, LIBRARY[4].steps[0])
    (count,) = run(db, "SELECT COUNT(*) AS n FROM products", {})
    return {
        "unsold_count": len(rows),
        "unsold_name": rows[0]["product_name"],
        "product_count": count["n"],
        "sold_count": count["n"] - len(rows),
    }


def empty_reference() -> Claims:
    in_category = [p for p in PRODUCTS if p.category == "Swimwear"]
    return {
        "swimwear_products": len(in_category),
        "category_count": len({p.category for p in PRODUCTS}),
    }


def empty_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    sales, exists, categories = (run_step(db, s) for s in LIBRARY[5].steps)
    # An ungrouped aggregate over nothing is one NULL row, never a zero.
    assert sales == [{"completed_item_sales": None}]
    assert {r["category"] for r in categories} == {
        "Accessories",
        "Bottoms",
        "Dresses",
        "Footwear",
        "Outerwear",
        "Tops",
    }
    return {"swimwear_products": len(exists), "category_count": len(categories)}


def customers_reference() -> Claims:
    sold = select(JUL_SEP.start, JUL_SEP.end)
    by_customer: dict[str, list[SalesItem]] = defaultdict(list)
    for item in sold:
        by_customer[item.customer_ref].append(item)
    ranked = sorted(by_customer.values(), key=lambda items: -total(items))
    all_total = metric("completed_item_sales", JUL_SEP)
    top3 = sum((total(items) for items in ranked[:3]), Decimal(0))
    return {
        "customers": metric("purchasing_customers", JUL_SEP),
        "sales_per_customer": money(metric("sales_per_customer", JUL_SEP)),
        "top1_sales": total(ranked[0]),
        "all_sales": all_total,
        "top1_share_pct": pct(total(ranked[0]), all_total),
        "top3_sales": top3,
        "top3_share_pct": pct(top3, all_total),
        "smallest_top_sales": total(ranked[9]),
        "top1_orders": orders(ranked[0]),
    }


def customers_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    rows = run_step(db, LIBRARY[6].steps[0])
    (overall,) = run_step(db, LIBRARY[6].steps[1])
    all_total = money(overall["completed_item_sales"])
    sales = [money(r["completed_item_sales"]) for r in rows]
    return {
        "customers": overall["purchasing_customers"],
        "sales_per_customer": money(overall["sales_per_customer"]),
        "top1_sales": sales[0],
        "all_sales": all_total,
        "top1_share_pct": pct(sales[0], all_total),
        "top3_sales": sum(sales[:3], Decimal(0)),
        "top3_share_pct": pct(sum(sales[:3], Decimal(0)), all_total),
        "smallest_top_sales": sales[-1],
        "top1_orders": rows[0]["completed_orders"],
    }


STATE_KEYS = {
    "Berlin": "berlin",
    "California": "california",
    "Texas": "texas",
    "New York": "newyork",
    "Washington": "washington",
    "Bavaria": "bavaria",
    "Florida": "florida",
}
BAND_KEYS = {"30-49": "age_30_49", "50+": "age_50", "under 30": "age_u30"}


def demographics_reference() -> Claims:
    sold = select(JUL_SEP.start, JUL_SEP.end)
    by_state: dict[str, list[SalesItem]] = defaultdict(list)
    by_band: dict[str, list[SalesItem]] = defaultdict(list)
    for item in sold:
        customer = CUSTOMER[item.customer_ref]
        by_state[customer.state].append(item)
        by_band[age_band(customer.age)].append(item)

    def people(items: list[SalesItem]) -> int:
        return len({i.customer_ref for i in items})

    claims: Claims = {}
    for state, key in STATE_KEYS.items():
        items = by_state[state]
        claims[f"{key}_sales"] = total(items)
        claims[f"{key}_spc"] = money(total(items) / people(items))
    for band, key in BAND_KEYS.items():
        items = by_band[band]
        claims[f"{key}_customers"] = people(items)
        claims[f"{key}_sales"] = total(items)
        claims[f"{key}_spc"] = money(total(items) / people(items))
    claims["ratio"] = str(
        (claims["age_30_49_spc"] / claims["age_u30_spc"]).quantize(
            TENTH, rounding=ROUND_HALF_UP
        )
    )
    return claims


def demographics_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    states = {r["state"]: r for r in run_step(db, LIBRARY[7].steps[0])}
    bands = {r["age_band"]: r for r in run_step(db, LIBRARY[7].steps[1])}
    claims: Claims = {}
    for state, key in STATE_KEYS.items():
        claims[f"{key}_sales"] = money(states[state]["completed_item_sales"])
        claims[f"{key}_spc"] = money(states[state]["sales_per_customer"])
    for band, key in BAND_KEYS.items():
        claims[f"{key}_customers"] = bands[band]["purchasing_customers"]
        claims[f"{key}_sales"] = money(bands[band]["completed_item_sales"])
        claims[f"{key}_spc"] = money(bands[band]["sales_per_customer"])
    claims["ratio"] = str(
        (claims["age_30_49_spc"] / claims["age_u30_spc"]).quantize(
            TENTH, rounding=ROUND_HALF_UP
        )
    )
    return claims


PRODUCT_KEYS = {
    "Gamma Dress": "dress",
    "Beta Shirt": "shirt",
    "Alpha Jacket": "jacket",
    "Beta Jeans": "jeans",
    "Alpha Tee": "tee",
    "Delta Boots": "boots",
    "Gamma Scarf": "scarf",
}
AUG = DateWindow(date(2026, 8, 1), date(2026, 9, 1))
SEP = DateWindow(date(2026, 9, 1), date(2026, 10, 1))


def contributors_reference() -> Claims:
    aug_total = metric("completed_item_sales", AUG)
    sep_total = metric("completed_item_sales", SEP)
    by_name = {p.name: str(p.product_id) for p in PRODUCTS}
    claims: Claims = {"aug_sales": aug_total, "sep_sales": sep_total}
    claims["fall"] = sep_total - aug_total
    others = Decimal(0)
    for name, key in PRODUCT_KEYS.items():
        aug = total(
            i for i in select(AUG.start, AUG.end) if i.product_id == by_name[name]
        )
        sep = total(
            i for i in select(SEP.start, SEP.end) if i.product_id == by_name[name]
        )
        claims[f"{key}_aug"], claims[f"{key}_sep"] = aug, sep
        claims[f"{key}_change"] = sep - aug
        if key != "dress":
            others += sep - aug
    claims["others_rise"] = others
    dress = by_name["Gamma Dress"]
    sep_items = [
        i for i in ITEMS if SEP.contains(i.ordered_date) and i.product_id == dress
    ]
    aug_items = [
        i for i in ITEMS if AUG.contains(i.ordered_date) and i.product_id == dress
    ]
    claims["dress_aug_items"] = len(
        [i for i in aug_items if i.item_status == "Complete"]
    )
    claims["dress_sep_items"] = len(
        [i for i in sep_items if i.item_status == "Complete"]
    )
    processing = total(i for i in sep_items if i.item_status == "Processing")
    claims["dress_processing"] = processing
    claims["sep_if_complete"] = sep_total + processing
    return claims


def contributors_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    months, products, status = (run_step(db, s) for s in LIBRARY[8].steps)
    aug_total = money(months[0]["completed_item_sales"])
    sep_total = money(months[1]["completed_item_sales"])
    claims: Claims = {"aug_sales": aug_total, "sep_sales": sep_total}
    claims["fall"] = sep_total - aug_total
    by_name = {r["product_name"]: r for r in products}
    others = Decimal(0)
    for name, key in PRODUCT_KEYS.items():
        row = by_name[name]
        claims[f"{key}_aug"] = money(row["base_sales"])
        claims[f"{key}_sep"] = money(row["compare_sales"])
        claims[f"{key}_change"] = money(row["change"])
        if key != "dress":
            others += money(row["change"])
    claims["others_rise"] = others
    cells = {(r["month"].month, r["item_status"]): r for r in status}
    claims["dress_aug_items"] = cells[(8, "Complete")]["items"]
    claims["dress_sep_items"] = cells[(9, "Complete")]["items"]
    processing = money(cells[(9, "Processing")]["sale_amount_total"])
    claims["dress_processing"] = processing
    claims["sep_if_complete"] = sep_total + processing
    # The biggest decliner in step 2 is the product step 3 inspects.
    assert products[0]["product_name"] == "Gamma Dress"
    return claims


def status_reference() -> Claims:
    window = select(JUL_SEP.start, JUL_SEP.end, status=None)
    by_status: dict[str, list[SalesItem]] = defaultdict(list)
    for item in window:
        by_status[str(item.item_status)].append(item)
    complete = total(by_status["Complete"])
    everything = total(window)
    return {
        "complete_items": len(by_status["Complete"]),
        "complete_sales": complete,
        "returned_items": len(by_status["Returned"]),
        "returned_sales": total(by_status["Returned"]),
        "processing_sales": total(by_status["Processing"]),
        "shipped_sales": total(by_status["Shipped"]),
        "cancelled_sales": total(by_status["Cancelled"]),
        "all_items": len(window),
        "all_sales": everything,
        "complete_share_pct": pct(complete, everything),
        "excluded_sales": everything - complete,
        "in_flight_sales": total(by_status["Processing"]) + total(by_status["Shipped"]),
        "final_excluded_sales": total(by_status["Returned"])
        + total(by_status["Cancelled"]),
        "with_shipped": complete + total(by_status["Shipped"]),
    }


def status_sql(db: duckdb.DuckDBPyConnection) -> Claims:
    rows = {r["item_status"]: r for r in run_step(db, LIBRARY[9].steps[0])}
    amount = {k: money(v["sale_amount_total"]) for k, v in rows.items()}
    everything = sum(amount.values(), Decimal(0))
    complete = amount["Complete"]
    return {
        "complete_items": rows["Complete"]["items"],
        "complete_sales": complete,
        "returned_items": rows["Returned"]["items"],
        "returned_sales": amount["Returned"],
        "processing_sales": amount["Processing"],
        "shipped_sales": amount["Shipped"],
        "cancelled_sales": amount["Cancelled"],
        "all_items": sum(r["items"] for r in rows.values()),
        "all_sales": everything,
        "complete_share_pct": pct(complete, everything),
        "excluded_sales": everything - complete,
        "in_flight_sales": amount["Processing"] + amount["Shipped"],
        "final_excluded_sales": amount["Returned"] + amount["Cancelled"],
        "with_shipped": complete + amount["Shipped"],
    }


ROUTES: dict[
    str,
    tuple[Callable[[], Claims], Callable[[duckdb.DuckDBPyConnection], Claims]],
] = {
    "monthly-revenue-trend": (trend_reference, trend_sql),
    "partial-month-comparison": (partial_reference, partial_sql),
    "top-products-by-revenue": (top_products_reference, top_products_sql),
    "brand-comparison-orders": (brand_reference, brand_sql),
    "products-without-sales": (unsold_reference, unsold_sql),
    "empty-result-diagnosis": (empty_reference, empty_sql),
    "customer-spending-concentration": (customers_reference, customers_sql),
    "spend-by-state-and-age-band": (demographics_reference, demographics_sql),
    "revenue-drop-contributors": (contributors_reference, contributors_sql),
    "order-status-mix": (status_reference, status_sql),
}


def normalised(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value.quantize(CENTS)
    return value


def test_every_example_has_a_figure_check() -> None:
    assert {e.key for e in LIBRARY} == set(ROUTES)


@pytest.mark.parametrize("example", LIBRARY, ids=lambda e: e.key)
def test_claims_match_both_independent_routes(
    example: SeedExample, db: duckdb.DuckDBPyConnection
) -> None:
    reference_route, sql_route = ROUTES[example.key]
    declared = {c.claim_id: normalised(c.value) for c in example.claims}
    by_python = {k: normalised(v) for k, v in reference_route().items()}
    by_sql = {k: normalised(v) for k, v in sql_route(db).items()}
    assert by_python == declared
    assert by_sql == declared


def test_hand_checked_anchor_figures() -> None:
    """Totals added up by hand from the fixture orders, not from any code."""
    july = 22 + 14 + 75 + 55 + 38 + 85 + 110 + 14
    august = 85 + 24 + 58 + 115 + 40 + 78 + 15 + 23 + 88
    september = 80 + 60 + 25 + 15 + 90 + 40 + 25 + 15 + 120
    assert (july, august, september) == (413, 526, 470)
    assert metric("completed_item_sales", month_window(2026, 7)) == july
    assert metric("completed_item_sales", month_window(2026, 8)) == august
    assert metric("completed_item_sales", month_window(2026, 9)) == september
    assert july + august + september == 1409


# -- sanitization and wording --------------------------------------------------


@pytest.mark.parametrize("example", LIBRARY, ids=lambda e: e.key)
def test_example_passes_the_golden_sanitization_screen(example: SeedExample) -> None:
    fields = {
        "question": example.question,
        "sql": example.sql,
        "method_summary": example.method_summary,
        "report": example.report_markdown,
    }
    assert screen_fields(fields) == ()


@pytest.mark.parametrize("example", LIBRARY, ids=lambda e: e.key)
def test_example_carries_no_identity_or_currency_or_experience_claims(
    example: SeedExample,
) -> None:
    text = "\n".join([example.question, example.sql, example.report_markdown]).lower()
    for symbol in "$€£¥":
        assert symbol not in text
    for banned in (
        "first_name",
        "last_name",
        "street_address",
        "@example",
        "customer_ref-",
        "ref-",
        "years of experience",
        "in my experience",
        "we found",
    ):
        assert banned not in text
    # customers appear only through the opaque reference field
    assert not re.search(r"\b(?:user_id|customer_id|email)\b", example.sql, re.I)


# -- loader: publication through the real lifecycle ----------------------------


class _Directory:
    def __init__(self, people: dict[str, ExecutiveAccess]) -> None:
        self._people = people

    async def find_by_subject(self, issuer: str, subject: str) -> None:
        return None

    async def get(self, executive_id: str) -> ExecutiveAccess | None:
        return self._people.get(executive_id)


class _NoRecords:
    async def get_session(self, session_id: str) -> None:
        return None

    async def get_run(self, run_id: str) -> None:
        return None

    async def get(self, operation_id: str) -> None:
        return None


def _executive(name: str, roles: set[Role]) -> ExecutiveAccess:
    return ExecutiveAccess(
        executive_id=name,
        roles=frozenset(roles),
        product_ids=frozenset({"1"}),
        active=True,
        authorization_version=1,
    )


class Lab:
    def __init__(self, tmp_path: Path) -> None:
        people = {
            "exec-demo-a": _executive("exec-demo-a", {Role.EXECUTIVE, Role.EDITOR}),
            "exec-demo-b": _executive("exec-demo-b", {Role.EXECUTIVE, Role.REVIEWER}),
        }
        resolver = AccessResolver(
            _Directory(people), OwnershipGuard(_NoRecords(), _NoRecords(), _NoRecords())
        )
        blobs = LocalBlobStore(tmp_path / "artifacts")
        catalog = MemoryCatalog()
        artifacts = ArtifactService(catalog, blobs, ArtifactPolicy())
        self.repository = MemoryKnowledgeRepository()
        self.service = KnowledgeService(
            resolver,
            self.repository,
            artifacts,
            ArtifactMaintenance(catalog, blobs),
            clock=lambda: NOW,
        )
        self.reader = GoldenKnowledgeReader(self.repository, artifacts)
        self.author, self.reviewer = seed_principals("exec-demo-a", "exec-demo-b")


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


def _seed(lab: Lab) -> Any:
    return asyncio.run(seed_golden_library(lab.service, lab.author, lab.reviewer))


def test_seeding_publishes_every_seed_through_independent_review(lab: Lab) -> None:
    results = _seed(lab)
    assert [r.outcome for r in results] == [SeedOutcome.PUBLISHED] * len(LIBRARY)
    assert all(r.status is ReviewStatus.PUBLISHED for r in results)
    for result, example in zip(results, LIBRARY, strict=True):
        version = lab.repository.versions[result.ref]
        assert result.ref == ExampleRef(example.example_id, 1)
        assert version.origin is Origin.PROJECT_AUTHORED
        assert version.provenance.source_kind is SourceKind.AUTHORED
        assert version.access.is_shared
        assert version.author_id == "exec-demo-a"
        assert version.reviewed_by == "exec-demo-b"
        assert version.applicability.schema_version == SEED_SCHEMA_VERSION
        assert version.applicability.metrics == example.metrics
        events = asyncio.run(lab.repository.events(version.ref))
        assert [e.action for e in events] == [ReviewAction.SUBMIT, ReviewAction.APPROVE]
        approval = events[-1]
        assert approval.actor_id == "exec-demo-b"
        assert approval.checks == {
            "correct": True,
            "sanitized": True,
            "applicable": True,
        }
        assert "not a historical analyst record" in approval.rationale


def test_seeding_is_idempotent(lab: Lab) -> None:
    first = _seed(lab)
    versions = dict(lab.repository.versions)
    events = len(lab.repository.review_events)
    index_events = len(lab.repository.index_events)
    again = _seed(lab)
    assert [r.outcome for r in again] == [SeedOutcome.ALREADY_PUBLISHED] * len(LIBRARY)
    assert [r.ref for r in again] == [r.ref for r in first]
    assert lab.repository.versions == versions
    assert len(lab.repository.review_events) == events
    assert len(lab.repository.index_events) == index_events


def test_seeding_finishes_an_interrupted_run(lab: Lab) -> None:
    from retail_analytics.application.golden_seeding import draft_for

    example = LIBRARY[0]
    asyncio.run(
        lab.service.submit_candidate(
            lab.author,
            draft_for(example),
            idempotency_key=example.idempotency_key,
            example_id=example.example_id,
        )
    )
    results = _seed(lab)
    assert results[0].outcome is SeedOutcome.PUBLISHED
    assert {r.status for r in results} == {ReviewStatus.PUBLISHED}


def test_seeding_never_overrides_a_later_review_decision(lab: Lab) -> None:
    results = _seed(lab)
    suspended = results[0].ref
    assert suspended is not None
    asyncio.run(lab.service.suspend(lab.reviewer, suspended, rationale="Under review."))
    again = _seed(lab)
    assert again[0].outcome is SeedOutcome.LEFT_UNCHANGED
    assert again[0].status is ReviewStatus.SUSPENDED
    assert {r.outcome for r in again[1:]} == {SeedOutcome.ALREADY_PUBLISHED}


def test_the_author_identity_cannot_publish_its_own_seed(lab: Lab) -> None:
    from retail_analytics.application.authorization import AccessDenied
    from retail_analytics.application.golden_seeding import (
        APPROVAL_RATIONALE,
        draft_for,
    )
    from retail_analytics.application.knowledge import ApprovalChecks
    from retail_analytics.domain.access import Permission

    candidate = asyncio.run(
        lab.service.submit_candidate(
            lab.author,
            draft_for(LIBRARY[0]),
            idempotency_key=LIBRARY[0].idempotency_key,
            example_id=LIBRARY[0].example_id,
        )
    )
    as_reviewer = Principal(
        "exec-demo-a", frozenset({Permission.KNOWLEDGE_REVIEW.value})
    )
    # The author's server-side roles lack review, whatever the token scopes say.
    with pytest.raises(AccessDenied):
        asyncio.run(
            lab.service.approve(
                as_reviewer,
                candidate.ref,
                rationale=APPROVAL_RATIONALE,
                checks=ApprovalChecks(True, True, True),
            )
        )
    assert lab.repository.versions[candidate.ref].status is ReviewStatus.CANDIDATE


def test_changed_content_without_a_revision_bump_is_reported(lab: Lab) -> None:
    from dataclasses import replace

    _seed(lab)
    edited = replace(LIBRARY[0], summary=LIBRARY[0].summary + " Extra sentence.")
    results = asyncio.run(
        seed_golden_library(
            lab.service, lab.author, lab.reviewer, library=[edited, *LIBRARY[1:]]
        )
    )
    assert results[0].outcome is SeedOutcome.CONTENT_CONFLICT
    assert {r.outcome for r in results[1:]} == {SeedOutcome.ALREADY_PUBLISHED}


def test_an_unfit_library_submits_nothing(lab: Lab) -> None:
    from dataclasses import replace

    broken = replace(LIBRARY[0], claims=LIBRARY[0].claims[:-1])
    with pytest.raises(SeedLibraryInvalid):
        asyncio.run(
            seed_golden_library(
                lab.service, lab.author, lab.reviewer, library=[broken, *LIBRARY[1:]]
            )
        )
    assert lab.repository.versions == {}


def test_published_seeds_reach_the_model_only_through_the_checked_reader(
    lab: Lab,
) -> None:
    results = _seed(lab)
    candidates = [
        (r.ref, lab.repository.versions[r.ref].content_digest) for r in results if r.ref
    ]
    context = ApplicabilityContext(
        SEED_SCHEMA_VERSION,
        {m: CATALOG.latest_version(m) for m in CATALOG.metric_ids()},
    )
    delivery = asyncio.run(
        lab.reader.deliver(ProductScope(frozenset(), 0), context, candidates)
    )
    assert len(delivery.examples) == len(LIBRARY) and not delivery.refused
    by_ref = {e.ref: e for e in delivery.examples}
    for result, example in zip(results, LIBRARY, strict=True):
        assert result.ref is not None
        delivered = by_ref[result.ref]
        assert delivered.question == example.question
        assert delivered.sql == example.sql
        assert delivered.report_markdown == example.report_markdown
        assert delivered.origin is Origin.PROJECT_AUTHORED
        assert delivered.shared
    incompatible = ApplicabilityContext(
        "logical-catalog/999", dict(context.metric_versions)
    )
    refused = asyncio.run(
        lab.reader.deliver(ProductScope(frozenset(), 0), incompatible, candidates)
    )
    assert not refused.examples and len(refused.refused) == len(LIBRARY)
    drifted = ApplicabilityContext(SEED_SCHEMA_VERSION, {"completed_item_sales": 2})
    assert not asyncio.run(
        lab.reader.deliver(ProductScope(frozenset(), 0), drifted, candidates)
    ).examples


def test_the_screen_would_block_a_seed_with_identifying_content(lab: Lab) -> None:
    from dataclasses import replace

    from retail_analytics.application.golden_seeding import draft_for

    dirty = replace(draft_for(LIBRARY[0]), report_markdown="Contact jo@example.com now")
    with pytest.raises(KnowledgeError) as blocked:
        asyncio.run(
            lab.service.submit_candidate(lab.author, dirty, idempotency_key="dirty-1")
        )
    assert blocked.value.code is KnowledgeErrorCode.SENSITIVE_CONTENT
