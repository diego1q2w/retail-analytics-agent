# ruff: noqa: S608
# The SQL here is constant teaching text assembled from constant fragments;
# nothing in this module executes it or interpolates caller input.
"""The project-authored Golden seed library: ten Question -> SQL -> Report trios.

These are teaching examples written for this project from the metric catalog
and the logical catalog. They are labelled ``Origin.PROJECT_AUTHORED``; they
are not records of past analyst work and carry no author experience. Every
figure in a report comes from the synthetic reference fixture
``golden-seed-fixture/1`` (see ``tests/golden_seed_fixture.py``), is labelled
as illustrative, and is recomputed by the test suite two independent ways.

An example teaches a method. A new question still needs fresh queries through
the guarded executor against the current permitted data; nothing here is
evidence about a real period or population.

Content rules the tests enforce: logical SQL only (compiles against catalog
v1), revenue means ``completed_item_sales`` (status exactly ``Complete``),
windows are half-open UTC complete days passed as parameters, no currency
symbol, no personal data, no raw identifiers, and every number in a report is
a declared, verified claim.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from retail_analytics.application.query_compiler import ScalarValue
from retail_analytics.domain.catalog import LogicalCatalog
from retail_analytics.domain.knowledge import MetricRef
from retail_analytics.domain.logical_catalog import CATALOG_VERSION
from retail_analytics.domain.metrics import (
    AVERAGE_ORDER_SALES,
    COMPLETED_ITEM_SALES,
    COMPLETED_ITEMS,
    COMPLETED_ORDERS,
    PURCHASING_CUSTOMERS,
    SALES_PER_CUSTOMER,
    MetricCatalog,
)

# What every seed was written for. Delivery requires an exact match with the
# running catalog version and each referenced metric version.
SEED_SCHEMA_VERSION = f"logical-catalog/{CATALOG_VERSION}"
FIXTURE_ID = "golden-seed-fixture/1"
SEED_LIBRARY_REVISION = 1

SEED_AUTHOR_KEY = "demo-a"
SEED_REVIEWER_KEY = "demo-b"

_NOTICE = (
    f"> Illustrative worked example on the synthetic reference fixture "
    f"`{FIXTURE_ID}`. The figures show how to read a result; they are not "
    "about any real period, product or customer. For a real question, run "
    "fresh queries on the current permitted data. Currency is not verified, "
    "so amounts carry no symbol."
)


@dataclass(frozen=True, slots=True)
class Claim:
    """A figure quoted in the report and its independently checked value."""

    claim_id: str
    text: str
    value: Decimal | int | str


@dataclass(frozen=True, slots=True)
class Step:
    """One query of an example; each runs as its own tool call."""

    title: str
    sql: str
    parameters: Mapping[str, ScalarValue]


@dataclass(frozen=True, slots=True)
class SeedExample:
    key: str
    title: str
    themes: tuple[str, ...]
    question: str
    steps: tuple[Step, ...]
    summary: str
    report_body: str
    metrics: frozenset[MetricRef]
    claims: tuple[Claim, ...]
    # Small numbers in the prose that are not results (e.g. "three months").
    allowed_numbers: frozenset[str] = frozenset()

    @property
    def sql(self) -> str:
        return "\n\n".join(
            f"-- Step {n}: {step.title}\n{step.sql.strip()};"
            for n, step in enumerate(self.steps, start=1)
        )

    @property
    def method_summary(self) -> str:
        values: dict[str, ScalarValue] = {}
        for step in self.steps:
            values.update(step.parameters)
        shown = ", ".join(
            f"@{name}={_show(value)}" for name, value in sorted(values.items())
        )
        return (
            f"{self.summary}\n\nRun each step as its own query. Illustrative "
            f"parameter values in the worked example: {shown}. Derive the real "
            "values from the current question and the period conventions."
        )

    @property
    def report_markdown(self) -> str:
        return f"# {self.title}\n\n{_NOTICE}\n\n{self.report_body.strip()}\n"

    @property
    def example_id(self) -> str:
        """Stable across runs so reseeding finds the same example."""
        return hashlib.sha256(f"golden-seed:{self.key}".encode()).hexdigest()[:32]

    @property
    def idempotency_key(self) -> str:
        return f"seed-{self.key}-r{SEED_LIBRARY_REVISION}"


def _show(value: ScalarValue) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


_JULY_TO_SEPTEMBER: dict[str, ScalarValue] = {
    "window_start": date(2026, 7, 1),
    "window_end": date(2026, 10, 1),
}
_COMPLETE_WINDOW = (
    "WHERE item_status = 'Complete'\n"
    "  AND ordered_date >= @window_start AND ordered_date < @window_end"
)
_COMPLETE_WINDOW_S = (
    "WHERE s.item_status = 'Complete'\n"
    "  AND s.ordered_date >= @window_start AND s.ordered_date < @window_end"
)


def _m(metric_id: str) -> MetricRef:
    return MetricRef(metric_id, 1)


MONTHLY_TREND = SeedExample(
    key="monthly-revenue-trend",
    title="Revenue trend over the last three complete months",
    themes=("trends", "revenue"),
    question=(
        "How did revenue change month by month over the last three complete months?"
    ),
    steps=(
        Step(
            "completed-item sales and completed orders per month",
            f"""SELECT DATE_TRUNC(ordered_date, MONTH) AS month,
       SUM(sale_amount) AS completed_item_sales,
       COUNT(DISTINCT order_ref) AS completed_orders
FROM sales_items
{_COMPLETE_WINDOW}
GROUP BY month
ORDER BY month""",
            _JULY_TO_SEPTEMBER,
        ),
    ),
    summary=(
        "Revenue means completed_item_sales v1: the sum of sale_amount over "
        "items whose status is exactly Complete, dated by order date in UTC. "
        "Use a half-open window of complete calendar months "
        "(start inclusive, end exclusive) and group by month. Report the "
        "month-over-month change from the returned rows rather than "
        "re-querying, state the definition and window, and say that only "
        "permitted products are included."
    ),
    report_body="""
## Question
How did revenue change month by month over the last three complete months?

## Definition and scope
- Revenue is `completed_item_sales` v1: sum of `sale_amount` over items with
  status exactly `Complete`, by order date (UTC), within the permitted products.
  It is not net revenue: returns, refunds and cancellations are not deducted
  beyond excluding items that are not `Complete`.
- Window: 2026-07-01 up to but not including 2026-10-01, three complete months.
  An order on 2026-08-31 counts in August; one on 2026-09-01 counts in September.

## Findings (fixture)
| Month | Completed-item sales | Completed orders |
|---|---|---|
| July 2026 | 413 | 5 |
| August 2026 | 526 | 6 |
| September 2026 | 470 | 7 |

- August rose by 113 (+27.4%) over July.
- September fell by 56 (-10.6%) from August, even though completed orders
  rose from 6 to 7, so the value per order dropped.

## Assumptions and limits
- Status is the current status of each item, not its status at the time of the
  order. A later return can lower a past month, so a month's figure can change.
- A change between months says what moved, not why. See the multi-step
  contributors example for taking the next step without claiming a cause.
- Name the definition when the user says "revenue"; if they mean something
  else (gross, net, including shipped), ask or label it exploratory.
""",
    metrics=frozenset({_m(COMPLETED_ITEM_SALES), _m(COMPLETED_ORDERS)}),
    claims=(
        Claim("jul_sales", "413", Decimal(413)),
        Claim("aug_sales", "526", Decimal(526)),
        Claim("sep_sales", "470", Decimal(470)),
        Claim("jul_orders", "5", 5),
        Claim("aug_orders", "6", 6),
        Claim("sep_orders", "7", 7),
        Claim("aug_change", "113", Decimal(113)),
        Claim("aug_change_pct", "+27.4%", "27.4"),
        Claim("sep_change", "56", Decimal(-56)),
        Claim("sep_change_pct", "-10.6%", "-10.6"),
    ),
    allowed_numbers=frozenset({"3"}),
)

PARTIAL_PERIOD = SeedExample(
    key="partial-month-comparison",
    title="This month so far against a comparable elapsed period",
    themes=("trends", "revenue", "partial periods"),
    question="How is revenue tracking this month compared with last month?",
    steps=(
        Step(
            "sales in the elapsed days of this month and the same days last month",
            """SELECT SUM(CASE WHEN ordered_date >= @current_start
                 AND ordered_date < @current_end THEN sale_amount END)
         AS current_period_sales,
       SUM(CASE WHEN ordered_date >= @previous_start
                 AND ordered_date < @previous_end THEN sale_amount END)
         AS previous_comparable_sales
FROM sales_items
WHERE item_status = 'Complete'
  AND ordered_date >= @previous_start AND ordered_date < @current_end""",
            {
                "current_start": date(2026, 10, 1),
                "current_end": date(2026, 10, 8),
                "previous_start": date(2026, 9, 1),
                "previous_end": date(2026, 9, 8),
            },
        ),
    ),
    summary=(
        "A running month is incomplete. Count only finished days: the current "
        "day is partial, so the end of the current window is today's date, "
        "exclusive. Compare with the same number of elapsed days at the start "
        "of the previous month, never with the whole previous month. Label the "
        "result as an incomplete period and show both windows. Both sums use "
        "completed_item_sales v1 in one pass with conditional sums."
    ),
    report_body="""
## Question
How is revenue tracking this month compared with last month?

## Definition and scope
- Revenue is `completed_item_sales` v1 (status exactly `Complete`, by order
  date in UTC, permitted products only).
- INCOMPLETE period: the fixture's as-of date is 2026-10-08, so only the days
  2026-10-01 up to but not including 2026-10-08 (7 finished days) are observed.
  Today's orders are excluded because the day is not over.
- Comparison window: the same 7 elapsed days of the previous month,
  2026-09-01 up to but not including 2026-09-08.

## Findings (fixture)
- Observed so far this month: 250.
- Comparable days last month: 180.
- That is +70 (+38.9%) on a like-for-like basis.
- One order dated on the as-of day (25) is not counted until the day finishes.
- For contrast, comparing 250 with the whole of September (470) would suggest
  a collapse that is only an artefact of comparing 7 days with a full month.

## Assumptions and limits
- A fixed number of elapsed days compares unevenly if weekday mix differs; for
  a decision, also look at a longer trend.
- Say clearly that the month is incomplete and that the figure will change.
""",
    metrics=frozenset({_m(COMPLETED_ITEM_SALES)}),
    claims=(
        Claim("current_sales", "250", Decimal(250)),
        Claim("previous_sales", "180", Decimal(180)),
        Claim("change", "+70", Decimal(70)),
        Claim("change_pct", "+38.9%", "38.9"),
        Claim("excluded_today", "25", Decimal(25)),
        Claim("full_september", "470", Decimal(470)),
    ),
    allowed_numbers=frozenset({"7"}),
)

TOP_PRODUCTS = SeedExample(
    key="top-products-by-revenue",
    title="Best-selling products by revenue",
    themes=("products", "revenue"),
    question="Which products brought in the most revenue over the last three months?",
    steps=(
        Step(
            "top five products by completed-item sales",
            f"""SELECT p.product_name, p.brand,
       SUM(s.sale_amount) AS completed_item_sales,
       COUNT(*) AS completed_items
FROM sales_items AS s
JOIN products AS p ON s.product_id = p.product_id
{_COMPLETE_WINDOW_S}
GROUP BY s.product_id, p.product_name, p.brand
ORDER BY completed_item_sales DESC, p.product_name
LIMIT 5""",
            _JULY_TO_SEPTEMBER,
        ),
        Step(
            "all completed-item sales in the same window, for the share",
            f"""SELECT SUM(sale_amount) AS completed_item_sales
FROM sales_items
{_COMPLETE_WINDOW}""",
            _JULY_TO_SEPTEMBER,
        ),
    ),
    summary=(
        "Join sales_items to products on product_id, group by the product's "
        "identifier as well as its name, rank by completed_item_sales, and "
        "keep the ranking and the unit counts side by side so a high-volume "
        "low-price product is not mistaken for a top earner. Run the total as "
        "a second query to express the top group as a share. Revenue is "
        "completed_item_sales v1; catalog_price is a list price, not a "
        "realised sale amount, so do not rank by it."
    ),
    report_body="""
## Question
Which products brought in the most revenue over the last three months?

## Definition and scope
- Revenue is `completed_item_sales` v1 and units are `completed_items` v1
  (status exactly `Complete`). Window: 2026-07-01 up to but not including
  2026-10-01, UTC, permitted products only.
- Ranking uses realised `sale_amount`, not the product list price.

## Findings (fixture)
| Rank | Product | Brand | Completed-item sales | Completed items |
|---|---|---|---|---|
| 1 | Gamma Dress | Gamma | 348 | 4 |
| 2 | Delta Boots | Delta | 345 | 3 |
| 3 | Alpha Jacket | Alpha | 233 | 3 |
| 4 | Beta Jeans | Beta | 173 | 3 |
| 5 | Alpha Tee | Alpha | 119 | 5 |

- The five products total 1218 of 1409 completed-item sales, 86.4% of the whole.
- The top two are separated by 3 only, so treat their order as a near tie.
- Alpha Tee sold the most units (5) but ranks fifth by revenue: volume and
  revenue answer different questions, and the report should say which was asked.

## Assumptions and limits
- Products with no completed sale in the window do not appear here; see the
  unsold-products example for that question.
- Ranking says what sold, not why. Do not attribute it to price, promotion or
  demand without further evidence.
""",
    metrics=frozenset({_m(COMPLETED_ITEM_SALES), _m(COMPLETED_ITEMS)}),
    claims=(
        Claim("p1_sales", "348", Decimal(348)),
        Claim("p2_sales", "345", Decimal(345)),
        Claim("p3_sales", "233", Decimal(233)),
        Claim("p4_sales", "173", Decimal(173)),
        Claim("p5_sales", "119", Decimal(119)),
        Claim("p1_items", "4", 4),
        Claim("p2_items", "3", 3),
        Claim("p3_items", "3", 3),
        Claim("p4_items", "3", 3),
        Claim("p5_items", "5", 5),
        Claim("top5_total", "1218", Decimal(1218)),
        Claim("all_total", "1409", Decimal(1409)),
        Claim("top5_share_pct", "86.4%", "86.4"),
        Claim("gap_top_two", "3", Decimal(3)),
    ),
    allowed_numbers=frozenset({"1", "2", "3", "4", "5"}),
)

BRAND_COMPARISON = SeedExample(
    key="brand-comparison-orders",
    title="Comparing brands without double-counting orders",
    themes=("products", "revenue", "orders"),
    question=(
        "Compare our brands on revenue and average order value over the last "
        "three months."
    ),
    steps=(
        Step(
            "sales, orders and average order sales per brand",
            f"""SELECT p.brand,
       SUM(s.sale_amount) AS completed_item_sales,
       COUNT(DISTINCT s.order_ref) AS completed_orders,
       SAFE_DIVIDE(SUM(s.sale_amount), COUNT(DISTINCT s.order_ref))
         AS average_order_sales
FROM sales_items AS s
JOIN products AS p ON s.product_id = p.product_id
{_COMPLETE_WINDOW_S}
GROUP BY p.brand
ORDER BY completed_item_sales DESC""",
            _JULY_TO_SEPTEMBER,
        ),
        Step(
            "distinct orders overall, to compare with the per-brand counts",
            f"""SELECT COUNT(DISTINCT order_ref) AS completed_orders,
       SUM(sale_amount) AS completed_item_sales
FROM sales_items
{_COMPLETE_WINDOW}""",
            _JULY_TO_SEPTEMBER,
        ),
    ),
    summary=(
        "Group by brand through the products join. An order can contain "
        "items from several brands, so per-brand completed_orders overlap and "
        "do not add up to the overall count; sales do add up. "
        "average_order_sales v1 divides item sales by distinct orders and, "
        "within a brand, is the value of that brand's part of its orders, not "
        "the whole basket. Use SAFE_DIVIDE so a zero denominator gives no "
        "value instead of an error, and compute the overall figures in a "
        "second query to show the overlap."
    ),
    report_body="""
## Question
Compare our brands on revenue and average order value over the last three months.

## Definition and scope
- Revenue is `completed_item_sales` v1; orders are `completed_orders` v1
  (distinct orders with at least one completed permitted item); order value is
  `average_order_sales` v1 (completed-item sales per completed order).
- Window: 2026-07-01 up to but not including 2026-10-01, UTC, permitted
  products only.

## Findings (fixture)
| Brand | Completed-item sales | Completed orders | Average order sales |
|---|---|---|---|
| Gamma | 421 | 9 | 46.78 |
| Alpha | 352 | 8 | 44.00 |
| Delta | 345 | 3 | 115.00 |
| Beta | 291 | 5 | 58.20 |

- Sales add up to the overall 1409. Order counts do not: the brands show
  25 orders in total, but there are only 18 distinct completed orders, because
  some orders contain several brands. Never sum per-brand order counts.
- Overall average order sales is 78.28.
- Delta has the highest average order sales (115.00) from only 3 orders; with
  so few orders, one order moves the figure a lot.

## Assumptions and limits
- Per-brand average order sales measures only the brand's items in the order
  (a partial basket), so it is not comparable with the whole-order value.
- A ranking by average order sales and a ranking by total sales disagree here;
  say which one the user asked for and show both when it helps.
""",
    metrics=frozenset(
        {
            _m(COMPLETED_ITEM_SALES),
            _m(COMPLETED_ORDERS),
            _m(AVERAGE_ORDER_SALES),
        }
    ),
    claims=(
        Claim("gamma_sales", "421", Decimal(421)),
        Claim("alpha_sales", "352", Decimal(352)),
        Claim("delta_sales", "345", Decimal(345)),
        Claim("beta_sales", "291", Decimal(291)),
        Claim("gamma_orders", "9", 9),
        Claim("alpha_orders", "8", 8),
        Claim("delta_orders", "3", 3),
        Claim("beta_orders", "5", 5),
        Claim("gamma_aos", "46.78", Decimal("46.78")),
        Claim("alpha_aos", "44.00", Decimal("44.00")),
        Claim("delta_aos", "115.00", Decimal("115.00")),
        Claim("beta_aos", "58.20", Decimal("58.20")),
        Claim("all_sales", "1409", Decimal(1409)),
        Claim("brand_orders_sum", "25", 25),
        Claim("distinct_orders", "18", 18),
        Claim("overall_aos", "78.28", Decimal("78.28")),
    ),
)

UNSOLD_PRODUCTS = SeedExample(
    key="products-without-sales",
    title="Products with no completed sales in a period",
    themes=("products",),
    question="Which of our products did not sell at all last quarter?",
    steps=(
        Step(
            "permitted products with no completed item in the window",
            """SELECT product_id, product_name, category, brand
FROM products
WHERE product_id NOT IN (
  SELECT product_id
  FROM sales_items
  WHERE item_status = 'Complete'
    AND ordered_date >= @window_start AND ordered_date < @window_end)
ORDER BY product_id""",
            _JULY_TO_SEPTEMBER,
        ),
    ),
    summary=(
        "The products relation lists every permitted product, sold or not, so "
        "an anti-membership test against completed items in the window finds "
        "the ones with no completed sale. Do not join from sales_items to "
        "products for this: that drops the products that never sold. The "
        "answer is 'no completed sale in this window', which is not the same "
        "as 'never sold' or 'discontinued'."
    ),
    report_body="""
## Question
Which of our products did not sell at all last quarter?

## Definition and scope
- "Sold" means at least one item with status exactly `Complete`
  (`completed_items` v1 above zero) ordered in the window.
- Window: 2026-07-01 up to but not including 2026-10-01, UTC. Only permitted
  products are listed, so a product outside your scope is neither listed nor
  counted.

## Findings (fixture)
- 1 of the 8 permitted products had no completed sale: Delta Cap
  (Accessories, Delta).
- The other 7 each had at least one completed item in the window.

## Assumptions and limits
- Items returned, cancelled or still in progress do not count as sales, so a
  product whose only orders were not completed also appears here. Check the
  status mix before calling it unsold.
- This does not say why the product did not sell (no stock, delisted, low
  demand). Treat it as a list to investigate, not a conclusion.
""",
    metrics=frozenset({_m(COMPLETED_ITEMS)}),
    claims=(
        Claim("unsold_count", "1", 1),
        Claim("unsold_name", "Delta Cap", "Delta Cap"),
        Claim("product_count", "8", 8),
        Claim("sold_count", "7", 7),
    ),
)

EMPTY_RESULT = SeedExample(
    key="empty-result-diagnosis",
    title="Reading an empty or null result before answering",
    themes=("empty results", "revenue", "products"),
    question="What was the revenue from the Swimwear category last quarter?",
    steps=(
        Step(
            "completed-item sales for the requested category",
            f"""SELECT SUM(s.sale_amount) AS completed_item_sales
FROM sales_items AS s
JOIN products AS p ON s.product_id = p.product_id
{_COMPLETE_WINDOW_S}
  AND p.category = @category""",
            {**_JULY_TO_SEPTEMBER, "category": "Swimwear"},
        ),
        Step(
            "does the category exist among the permitted products",
            """SELECT category, COUNT(*) AS product_count
FROM products
WHERE category = @category
GROUP BY category""",
            {"category": "Swimwear"},
        ),
        Step(
            "which categories are available",
            """SELECT category, COUNT(*) AS product_count
FROM products
GROUP BY category
ORDER BY category""",
            {},
        ),
    ),
    summary=(
        "An aggregate with no GROUP BY returns one row whose sum is NULL when "
        "nothing qualifies; that is unknown or absent, not a revenue of zero. "
        "Before reporting, find out why it is empty: the category may not exist "
        "among the permitted products (check the products relation), or it "
        "may exist with no completed sale in the window (check the same "
        "window without the category filter). Say which one it is, list "
        "available categories, and ask whether the user meant one of them. "
        "Do not report zero, and do not hint at products outside the scope."
    ),
    report_body="""
## Question
What was the revenue from the Swimwear category last quarter?

## Definition and scope
- Revenue is `completed_item_sales` v1 (status exactly `Complete`), window
  2026-07-01 up to but not including 2026-10-01, UTC, permitted products only.

## Findings (fixture)
1. The sales query returned a single row with no value (NULL), not zero.
2. The category check found 0 products named Swimwear among the permitted
   products.
3. The permitted products fall into 6 categories: Accessories, Bottoms,
   Dresses, Footwear, Outerwear and Tops.

So the empty result is explained by the category being absent from the
permitted products, not by a quiet quarter. The honest answer is that there is
no Swimwear category in the data available to you.

## What to tell the user
- State that no such category exists within their permitted products, and
  offer the available categories. Do not write "0" as if it were a measured
  revenue.
- Do not speculate that the category exists elsewhere or that it exists for
  someone else; the scope is not something to hint at.
- If the category had existed with no completed sales, say that instead, and
  show the status mix for those products.

## Assumptions and limits
- A spelling or wording difference (for example "Swim wear") also gives an
  empty result; offering the available categories lets the user correct it.
""",
    metrics=frozenset({_m(COMPLETED_ITEM_SALES)}),
    claims=(
        Claim("swimwear_products", "0", 0),
        Claim("category_count", "6", 6),
    ),
    allowed_numbers=frozenset({"1", "2", "3"}),
)

CUSTOMER_SPENDING = SeedExample(
    key="customer-spending-concentration",
    title="How concentrated is customer spending",
    themes=("customers", "revenue"),
    question=(
        "Who are our biggest customers and how much of our revenue do they account for?"
    ),
    steps=(
        Step(
            "top customers by completed-item sales (opaque references)",
            f"""SELECT customer_ref,
       SUM(sale_amount) AS completed_item_sales,
       COUNT(DISTINCT order_ref) AS completed_orders
FROM sales_items
{_COMPLETE_WINDOW}
GROUP BY customer_ref
ORDER BY completed_item_sales DESC, customer_ref
LIMIT 10""",
            _JULY_TO_SEPTEMBER,
        ),
        Step(
            "purchasing customers and sales per customer overall",
            f"""SELECT COUNT(DISTINCT customer_ref) AS purchasing_customers,
       SUM(sale_amount) AS completed_item_sales,
       SAFE_DIVIDE(SUM(sale_amount), COUNT(DISTINCT customer_ref))
         AS sales_per_customer
FROM sales_items
{_COMPLETE_WINDOW}""",
            _JULY_TO_SEPTEMBER,
        ),
    ),
    summary=(
        "Customers are identified only by opaque references, never by name, "
        "email or raw identifier; do not try to resolve or print them. Rank by "
        "completed_item_sales within the permitted products and report "
        "concentration as shares of the total from a second query. "
        "purchasing_customers and sales_per_customer v1 count only customers "
        "with a completed permitted purchase. Describe customers by rank "
        "(top customer, top three) in the report; offer drill-down by "
        "reference instead of listing them."
    ),
    report_body="""
## Question
Who are our biggest customers and how much of our revenue do they account for?

## Definition and scope
- Revenue is `completed_item_sales` v1; customers are `purchasing_customers`
  v1 (distinct customers with a completed purchase of permitted products);
  `sales_per_customer` v1 divides the two.
- Window: 2026-07-01 up to but not including 2026-10-01, UTC. Customers are
  seen only through opaque references, and only through permitted products,
  so a customer's spend elsewhere is not visible.

## Findings (fixture)
- 11 customers made a completed purchase; sales per customer is 128.09.
- The top customer accounts for 275 of 1409 (19.5%); the top three account
  for 683 (48.5%). Spending is concentrated, with 3 customers making almost
  half of the sales.
- The smallest customer spend in the top list is 23, so the spread is wide.
- Individuals are not named in this report. Say "the top customer" and offer
  a drill-down by opaque reference if the user needs one.

## Assumptions and limits
- Spend is permitted-products-only and completed-only; it is not a customer's
  lifetime value.
- Concentration in a short window can reflect one large order. Check the
  order count (the top customer has 2 orders) before calling a customer a
  habitual big spender.
""",
    metrics=frozenset(
        {
            _m(COMPLETED_ITEM_SALES),
            _m(PURCHASING_CUSTOMERS),
            _m(SALES_PER_CUSTOMER),
            _m(COMPLETED_ORDERS),
        }
    ),
    claims=(
        Claim("customers", "11", 11),
        Claim("sales_per_customer", "128.09", Decimal("128.09")),
        Claim("top1_sales", "275", Decimal(275)),
        Claim("all_sales", "1409", Decimal(1409)),
        Claim("top1_share_pct", "19.5%", "19.5"),
        Claim("top3_sales", "683", Decimal(683)),
        Claim("top3_share_pct", "48.5%", "48.5"),
        Claim("smallest_top_sales", "23", Decimal(23)),
        Claim("top1_orders", "2", 2),
    ),
    allowed_numbers=frozenset({"3"}),
)

DEMOGRAPHICS = SeedExample(
    key="spend-by-state-and-age-band",
    title="Spending by customer state and age band",
    themes=("demographics", "customers"),
    question="Which customer states and age groups spend the most per customer?",
    steps=(
        Step(
            "sales per customer by country and state",
            f"""SELECT c.country, c.state,
       COUNT(DISTINCT s.customer_ref) AS purchasing_customers,
       SUM(s.sale_amount) AS completed_item_sales,
       SAFE_DIVIDE(SUM(s.sale_amount), COUNT(DISTINCT s.customer_ref))
         AS sales_per_customer
FROM sales_items AS s
JOIN customers AS c ON s.customer_ref = c.customer_ref
{_COMPLETE_WINDOW_S}
GROUP BY c.country, c.state
ORDER BY sales_per_customer DESC, c.state""",
            _JULY_TO_SEPTEMBER,
        ),
        Step(
            "sales per customer by age band",
            f"""SELECT c.age_band,
       COUNT(DISTINCT s.customer_ref) AS purchasing_customers,
       SUM(s.sale_amount) AS completed_item_sales,
       SAFE_DIVIDE(SUM(s.sale_amount), COUNT(DISTINCT s.customer_ref))
         AS sales_per_customer
FROM sales_items AS s
JOIN customers AS c ON s.customer_ref = c.customer_ref
{_COMPLETE_WINDOW_S}
GROUP BY c.age_band
ORDER BY c.age_band""",
            _JULY_TO_SEPTEMBER,
        ),
    ),
    summary=(
        "Demographics are allowed for individuals and populations: use the "
        "customers relation's country, state and age_band. Exact ages and "
        "finer location are not available and must not be reconstructed; use "
        "the bands the catalog provides and group by them as given. Show the "
        "number of customers behind each figure because small groups are "
        "noisy, even though no minimum group size is enforced. Do not call "
        "demographic combinations anonymous, and keep customer references out "
        "of the report."
    ),
    report_body="""
## Question
Which customer states and age groups spend the most per customer?

## Definition and scope
- `sales_per_customer` v1: `completed_item_sales` v1 divided by
  `purchasing_customers` v1, within permitted products. Window: 2026-07-01 up
  to but not including 2026-10-01, UTC.
- Country, state and age band come from the customers relation. Age is only
  available as a band chosen by the catalog; the band labels below are the
  fixture's own and real labels will differ.

## Findings (fixture)
| State | Customers | Completed-item sales | Sales per customer |
|---|---|---|---|
| Berlin | 1 | 275 | 275.00 |
| California | 2 | 403 | 201.50 |
| Texas | 2 | 228 | 114.00 |
| New York | 2 | 213 | 106.50 |
| Washington | 2 | 187 | 93.50 |
| Bavaria | 1 | 88 | 88.00 |
| Florida | 1 | 15 | 15.00 |

| Age band | Customers | Completed-item sales | Sales per customer |
|---|---|---|---|
| 30-49 | 4 | 777 | 194.25 |
| 50+ | 3 | 319 | 106.33 |
| under 30 | 4 | 313 | 78.25 |

- Berlin leads per customer, but it rests on a single customer; California
  (2 customers) is the better-supported leader.
- The 30-49 band spends about 2.5 times the under-30 band per customer.

## Assumptions and limits
- Customer counts are tiny here; with real data, show group sizes and avoid
  reading much into small ones.
- This describes who spent, not why. Do not infer motives from age or place.
- Do not describe these combinations as anonymous and do not drill down to
  identify individuals.
""",
    metrics=frozenset(
        {
            _m(COMPLETED_ITEM_SALES),
            _m(PURCHASING_CUSTOMERS),
            _m(SALES_PER_CUSTOMER),
        }
    ),
    claims=(
        Claim("berlin_sales", "275", Decimal(275)),
        Claim("berlin_spc", "275.00", Decimal("275.00")),
        Claim("california_sales", "403", Decimal(403)),
        Claim("california_spc", "201.50", Decimal("201.50")),
        Claim("texas_sales", "228", Decimal(228)),
        Claim("texas_spc", "114.00", Decimal("114.00")),
        Claim("newyork_sales", "213", Decimal(213)),
        Claim("newyork_spc", "106.50", Decimal("106.50")),
        Claim("washington_sales", "187", Decimal(187)),
        Claim("washington_spc", "93.50", Decimal("93.50")),
        Claim("bavaria_sales", "88", Decimal(88)),
        Claim("bavaria_spc", "88.00", Decimal("88.00")),
        Claim("florida_sales", "15", Decimal(15)),
        Claim("florida_spc", "15.00", Decimal("15.00")),
        Claim("age_30_49_customers", "4", 4),
        Claim("age_30_49_sales", "777", Decimal(777)),
        Claim("age_30_49_spc", "194.25", Decimal("194.25")),
        Claim("age_50_customers", "3", 3),
        Claim("age_50_sales", "319", Decimal(319)),
        Claim("age_50_spc", "106.33", Decimal("106.33")),
        Claim("age_u30_customers", "4", 4),
        Claim("age_u30_sales", "313", Decimal(313)),
        Claim("age_u30_spc", "78.25", Decimal("78.25")),
        Claim("ratio", "2.5", "2.5"),
    ),
    allowed_numbers=frozenset({"1", "2", "30", "49", "50"}),
)

CONTRIBUTORS = SeedExample(
    key="revenue-drop-contributors",
    title="Investigating a revenue drop: contributors, not causes",
    themes=("multi-step", "causal limits", "trends", "products"),
    question="Why did revenue fall in September?",
    steps=(
        Step(
            "confirm the change between the two complete months",
            """SELECT DATE_TRUNC(ordered_date, MONTH) AS month,
       SUM(sale_amount) AS completed_item_sales
FROM sales_items
WHERE item_status = 'Complete'
  AND ordered_date >= @base_start AND ordered_date < @compare_end
GROUP BY month
ORDER BY month""",
            {
                "base_start": date(2026, 8, 1),
                "compare_end": date(2026, 10, 1),
            },
        ),
        Step(
            "change in completed-item sales by product between the months",
            """SELECT p.product_name,
       SUM(CASE WHEN s.ordered_date >= @base_start
                 AND s.ordered_date < @base_end THEN s.sale_amount ELSE 0 END)
         AS base_sales,
       SUM(CASE WHEN s.ordered_date >= @compare_start
                 AND s.ordered_date < @compare_end THEN s.sale_amount ELSE 0 END)
         AS compare_sales,
       SUM(CASE WHEN s.ordered_date >= @compare_start
                 AND s.ordered_date < @compare_end THEN s.sale_amount ELSE 0 END)
       - SUM(CASE WHEN s.ordered_date >= @base_start
                 AND s.ordered_date < @base_end THEN s.sale_amount ELSE 0 END)
         AS change
FROM sales_items AS s
JOIN products AS p ON s.product_id = p.product_id
WHERE s.item_status = 'Complete'
  AND s.ordered_date >= @base_start AND s.ordered_date < @compare_end
GROUP BY p.product_name
ORDER BY change, p.product_name""",
            {
                "base_start": date(2026, 8, 1),
                "base_end": date(2026, 9, 1),
                "compare_start": date(2026, 9, 1),
                "compare_end": date(2026, 10, 1),
            },
        ),
        Step(
            "status mix of the biggest decliner in both months",
            """SELECT DATE_TRUNC(s.ordered_date, MONTH) AS month,
       s.item_status,
       COUNT(*) AS items,
       SUM(s.sale_amount) AS sale_amount_total
FROM sales_items AS s
JOIN products AS p ON s.product_id = p.product_id
WHERE p.product_name = @product_name
  AND s.ordered_date >= @base_start AND s.ordered_date < @compare_end
GROUP BY month, s.item_status
ORDER BY month, s.item_status""",
            {
                "product_name": "Gamma Dress",
                "base_start": date(2026, 8, 1),
                "compare_end": date(2026, 10, 1),
            },
        ),
    ),
    summary=(
        "Work from the observed change to the pieces that make it up, one "
        "query at a time: confirm the change, split it by product into base "
        "and compare sales and their difference, then look at the status mix "
        "of the largest contributor. Revenue is completed_item_sales v1, so "
        "an item that is not yet Complete is outside the figure; check "
        "whether the apparent drop is partly items still moving through "
        "statuses. The result identifies contributors to the change. It does "
        "not identify causes: say so, list what else would need to be "
        "checked, and do not state a reason the data cannot show."
    ),
    report_body="""
## Question
Why did revenue fall in September?

## Definition and scope
- Revenue is `completed_item_sales` v1 (status exactly `Complete`, by order
  date in UTC, permitted products only). Months compared: August (2026-08-01 up
  to but not including 2026-09-01) and September (2026-09-01 up to but not
  including 2026-10-01).

## Step 1: confirm the change
August was 526 and September 470, a fall of 56. The question's premise holds.

## Step 2: where did the change come from
| Product | August | September | Change |
|---|---|---|---|
| Gamma Dress | 173 | 90 | -83 |
| Beta Shirt | 40 | 40 | 0 |
| Alpha Jacket | 78 | 80 | +2 |
| Beta Jeans | 58 | 60 | +2 |
| Alpha Tee | 47 | 50 | +3 |
| Delta Boots | 115 | 120 | +5 |
| Gamma Scarf | 15 | 30 | +15 |

One product, Gamma Dress, fell by 83. The other products together rose by 27,
so the net fall is 56. Gamma Dress is the main contributor to the fall.

## Step 3: look inside the largest contributor
Gamma Dress had 2 completed items in August (173). In September it had
1 completed item (90) and 1 more item still in status Processing (90). If that
item completes, the September total becomes 560, above August (526), and the
drop disappears. The September figure is therefore provisional.

## What can and cannot be said
- Supported: the fall is concentrated in one product, and part of it is an
  order that had not completed at the time of the query.
- Not supported: any reason for the change (demand, stock, price, season,
  promotions, fulfilment delays). The data here shows where the change sits,
  not why it happened. Present the explanations as hypotheses to test, say
  what data would test each, and do not state one as the cause.

## Assumptions and limits
- Statuses are current, not historical, so the two months are not observed on
  equal footing: the later month has had less time to complete.
- A single-product story from one pair of months can be noise; check further
  months before calling it a pattern.
""",
    metrics=frozenset({_m(COMPLETED_ITEM_SALES), _m(COMPLETED_ITEMS)}),
    claims=(
        Claim("aug_sales", "526", Decimal(526)),
        Claim("sep_sales", "470", Decimal(470)),
        Claim("fall", "56", Decimal(-56)),
        Claim("dress_aug", "173", Decimal(173)),
        Claim("dress_sep", "90", Decimal(90)),
        Claim("dress_change", "-83", Decimal(-83)),
        Claim("shirt_aug", "40", Decimal(40)),
        Claim("shirt_sep", "40", Decimal(40)),
        Claim("shirt_change", "0", Decimal(0)),
        Claim("jacket_aug", "78", Decimal(78)),
        Claim("jacket_sep", "80", Decimal(80)),
        Claim("jacket_change", "+2", Decimal(2)),
        Claim("jeans_aug", "58", Decimal(58)),
        Claim("jeans_sep", "60", Decimal(60)),
        Claim("jeans_change", "+2", Decimal(2)),
        Claim("tee_aug", "47", Decimal(47)),
        Claim("tee_sep", "50", Decimal(50)),
        Claim("tee_change", "+3", Decimal(3)),
        Claim("boots_aug", "115", Decimal(115)),
        Claim("boots_sep", "120", Decimal(120)),
        Claim("boots_change", "+5", Decimal(5)),
        Claim("scarf_aug", "15", Decimal(15)),
        Claim("scarf_sep", "30", Decimal(30)),
        Claim("scarf_change", "+15", Decimal(15)),
        Claim("others_rise", "27", Decimal(27)),
        Claim("dress_aug_items", "2", 2),
        Claim("dress_sep_items", "1", 1),
        Claim("dress_processing", "90", Decimal(90)),
        Claim("sep_if_complete", "560", Decimal(560)),
    ),
    allowed_numbers=frozenset({"1", "2", "3"}),
)

STATUS_MIX = SeedExample(
    key="order-status-mix",
    title="What the revenue definition leaves out: the status mix",
    themes=("revenue", "definitions", "causal limits"),
    question=("How much of what customers ordered is not counted in revenue, and why?"),
    steps=(
        Step(
            "items and sale amount by item status",
            """SELECT item_status,
       COUNT(*) AS items,
       SUM(sale_amount) AS sale_amount_total
FROM sales_items
WHERE ordered_date >= @window_start AND ordered_date < @window_end
GROUP BY item_status
ORDER BY items DESC, item_status""",
            _JULY_TO_SEPTEMBER,
        ),
    ),
    summary=(
        "Group the same window by item_status without the Complete filter to "
        "see what the revenue definition excludes. Revenue stays "
        "completed_item_sales v1 (status exactly Complete); every other "
        "status, including ones still in progress, is outside it. Report the "
        "excluded amounts by status and keep any alternative definition, such "
        "as including shipped items, clearly labelled as exploratory and "
        "not an approved catalog metric. Statuses are current values and can "
        "change."
    ),
    report_body="""
## Question
How much of what customers ordered is not counted in revenue, and why?

## Definition and scope
- Revenue is `completed_item_sales` v1: status exactly `Complete`. Items in
  every other status are outside it, whatever the reason.
- Window: 2026-07-01 up to but not including 2026-10-01, UTC, permitted
  products only. Amounts are `sale_amount` summed per status.

## Findings (fixture)
| Item status | Items | Sale amount |
|---|---|---|
| Complete | 26 | 1409 |
| Returned | 2 | 102 |
| Processing | 1 | 90 |
| Shipped | 1 | 60 |
| Cancelled | 1 | 40 |

- Across all statuses there were 31 items worth 1701; completed items are
  1409 of that, 82.8% by amount.
- 292 of sale amount is outside revenue. Of it, 150 (Processing and Shipped)
  could still become completed; 142 (Returned and Cancelled) will not.

## Sensitivity, labelled exploratory
Including Shipped items would give 1469. This is an exploratory definition for
discussion only, not the approved revenue metric, and it must not replace
`completed_item_sales` in a report unless the user explicitly asks for it and
the report says so.

## Assumptions and limits
- Statuses are as of now. Recent months hold more not-yet-completed items than
  older ones, which flatters old months relative to new ones.
- The data records the status, not the reason. Do not explain a return or a
  cancellation beyond what the status says.
""",
    metrics=frozenset({_m(COMPLETED_ITEM_SALES), _m(COMPLETED_ITEMS)}),
    claims=(
        Claim("complete_items", "26", 26),
        Claim("complete_sales", "1409", Decimal(1409)),
        Claim("returned_items", "2", 2),
        Claim("returned_sales", "102", Decimal(102)),
        Claim("processing_sales", "90", Decimal(90)),
        Claim("shipped_sales", "60", Decimal(60)),
        Claim("cancelled_sales", "40", Decimal(40)),
        Claim("all_items", "31", 31),
        Claim("all_sales", "1701", Decimal(1701)),
        Claim("complete_share_pct", "82.8%", "82.8"),
        Claim("excluded_sales", "292", Decimal(292)),
        Claim("in_flight_sales", "150", Decimal(150)),
        Claim("final_excluded_sales", "142", Decimal(142)),
        Claim("with_shipped", "1469", Decimal(1469)),
    ),
    allowed_numbers=frozenset({"1"}),
)


def seed_library() -> tuple[SeedExample, ...]:
    return (
        MONTHLY_TREND,
        PARTIAL_PERIOD,
        TOP_PRODUCTS,
        BRAND_COMPARISON,
        UNSOLD_PRODUCTS,
        EMPTY_RESULT,
        CUSTOMER_SPENDING,
        DEMOGRAPHICS,
        CONTRIBUTORS,
        STATUS_MIX,
    )


_YEAR = re.compile(r"20\d\d")
_NUMBER = re.compile(r"(?<![\w.])[+-]?\d+(?:\.\d+)?%?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_VERSION_TAGS = re.compile(r"\bv\d+\b|[A-Za-z-]+/\d+|`[^`]*`|\bStep \d+")
_PROSE_FORBIDDEN = (
    "years of experience",
    "in production",
    "live-verified",
    "verified live",
    "our customers said",
)


def _plain_number(token: str) -> str:
    return token.lstrip("+-").rstrip("%")


def check_library(
    library: Sequence[SeedExample],
    logical: LogicalCatalog,
    metrics: MetricCatalog,
) -> list[str]:
    """Problems that make a seed unfit to publish; empty when it is fit.

    Pure checks only: shape, versions against the running catalogs, claims
    against the report text and report numbers against the declared claims.
    Running the SQL and recomputing the figures needs the test fixture and is
    done by the seed validation suite.
    """
    problems: list[str] = []
    if not 8 <= len(library) <= 12:
        problems.append(f"library has {len(library)} examples; expected 8-12")
    if len({e.key for e in library}) != len(library):
        problems.append("duplicate seed keys")
    schema = f"logical-catalog/{logical.version}"
    if schema != SEED_SCHEMA_VERSION:
        problems.append(f"schema {schema} differs from {SEED_SCHEMA_VERSION}")
    for example in library:
        problems += [f"{example.key}: {p}" for p in _check_example(example, metrics)]
    return problems


def _check_example(example: SeedExample, metrics: MetricCatalog) -> list[str]:
    problems: list[str] = []
    for ref in example.metrics:
        try:
            definition = metrics.get(ref.metric_id, ref.version)
        except KeyError:
            problems.append(f"unknown metric {ref.metric_id} v{ref.version}")
            continue
        if definition.is_exploratory:
            problems.append(f"{ref.metric_id} is exploratory")
    if not example.steps or not example.claims:
        problems.append("needs steps and claims")
    report = example.report_markdown
    ids = [c.claim_id for c in example.claims]
    if len(set(ids)) != len(ids):
        problems.append("duplicate claim ids")
    declared = {_plain_number(c.text) for c in example.claims}
    for claim in example.claims:
        if claim.text not in report:
            problems.append(f"claim {claim.claim_id} is not in the report")
    prose = _VERSION_TAGS.sub(" ", _ISO_DATE.sub(" ", report))
    for token in _NUMBER.findall(prose):
        number = _plain_number(token)
        if _YEAR.fullmatch(number):
            continue
        if number not in declared and number not in example.allowed_numbers:
            problems.append(f"report number {token} is not a declared claim")
    lowered = report.lower() + example.method_summary.lower()
    problems += [f"forbidden wording: {w}" for w in _PROSE_FORBIDDEN if w in lowered]
    if any(symbol in report for symbol in "$€£¥"):
        problems.append("currency symbol in report")
    return problems
