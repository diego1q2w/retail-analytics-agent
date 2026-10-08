# Golden seed library

Ten project-authored Question, SQL and Report examples that teach the agent
analytical methods. They are written for this project from the metric catalog
and the logical catalog. They are not records of past analyst work, they carry
no author experience, and every figure in them is illustrative.

- Origin label: `project_authored`; provenance: `authored`; access: shared.
- Written for schema version `logical-catalog/1` and the metric versions in the
  table. Delivery needs an exact match, so a catalog or metric change makes a
  seed ineligible until it is revised and reviewed again.
- Figures come from the synthetic reference fixture `golden-seed-fixture/1`
  (`tests/golden_seed_fixture.py`): invented products, placeholder customers
  and orders, no real rows. Currency is not verified, so amounts have no symbol.
- An example guides a method. New questions still need fresh queries against
  the current permitted data; a seed is never evidence about a real period.

## Corpus

| # | Key | Question | Themes | Queries | Metrics |
|---|---|---|---|---|---|
| 1 | `monthly-revenue-trend` | How did revenue change month by month over the last three complete months? | trends, revenue | 1 | `completed_item_sales` v1, `completed_orders` v1 |
| 2 | `partial-month-comparison` | How is revenue tracking this month compared with last month? | trends, revenue, partial periods | 1 | `completed_item_sales` v1 |
| 3 | `top-products-by-revenue` | Which products brought in the most revenue over the last three months? | products, revenue | 2 | `completed_item_sales` v1, `completed_items` v1 |
| 4 | `brand-comparison-orders` | Compare our brands on revenue and average order value over the last three months. | products, revenue, orders | 2 | `average_order_sales` v1, `completed_item_sales` v1, `completed_orders` v1 |
| 5 | `products-without-sales` | Which of our products did not sell at all last quarter? | products | 1 | `completed_items` v1 |
| 6 | `empty-result-diagnosis` | What was the revenue from the Swimwear category last quarter? | empty results, revenue, products | 3 | `completed_item_sales` v1 |
| 7 | `customer-spending-concentration` | Who are our biggest customers and how much of our revenue do they account for? | customers, revenue | 2 | `completed_item_sales` v1, `completed_orders` v1, `purchasing_customers` v1, `sales_per_customer` v1 |
| 8 | `spend-by-state-and-age-band` | Which customer states and age groups spend the most per customer? | demographics, customers | 2 | `completed_item_sales` v1, `purchasing_customers` v1, `sales_per_customer` v1 |
| 9 | `revenue-drop-contributors` | Why did revenue fall in September? | multi-step, causal limits, trends, products | 3 | `completed_item_sales` v1, `completed_items` v1 |
| 10 | `order-status-mix` | How much of what customers ordered is not counted in revenue, and why? | revenue, definitions, causal limits | 1 | `completed_item_sales` v1, `completed_items` v1 |

Coverage: trends (1, 2, 9), products (3, 4, 5), customers (7), demographics (8),
multi-step investigation (9), empty results (6) and causal limits (9, 10).

## Loading

```sh
retail-analytics-dev-access provision                  # demo-a (author) and demo-b (reviewer)
python -m retail_analytics.bootstrap.seed_knowledge    # needs migrations at head
```

Seeds go through the normal lifecycle: `demo-a` submits each as a candidate and
`demo-b`, a different identity with the reviewer role, approves it with the
recorded checks (correct, sanitized, applicable). Review history therefore shows
who did what, and the origin label stays honest.

The command is idempotent. Example ids and idempotency keys are stable, a repeat
returns the stored version, and only a version still waiting as a candidate is
approved. A later reviewer decision (suspend, reject, retire, erase) is reported
and left alone. To change a seed, edit it and raise `SEED_LIBRARY_REVISION`; the
next run submits version 2 of the same example, and approving it retires the old
one. Editing without raising the revision is reported as a content conflict.

## Validation

`tests/unit/test_golden_seeds.py` checks, for every seed:

- every query compiles against catalog v1 with the restricted compiler and runs
  on the fixture (DuckDB);
- revenue is `completed_item_sales` (status exactly `Complete`), windows are
  half-open UTC dates passed as parameters, and metric ids and versions are
  current and not exploratory;
- every number in a report is a declared claim, and each claim equals the value
  recomputed from the fixture rows in plain Python (with the reference metric
  semantics) and from the seed's own SQL;
- the Golden sanitization screen finds nothing, there is no currency symbol,
  identity field or experience claim;
- publication through the real lifecycle, with separate author and reviewer, is
  idempotent and delivers only through the checked reader.

`tests/integration/test_golden_seeds.py` (needs Docker, `-m docker`) runs the
command against PostgreSQL twice.

## Manual analytical review checklist

The automated checks do not replace this. A human reviewer applies it to each
seed before it is relied on outside development, and records the sign-off in the
review history.

1. The question is generic: no real names, dates that matter, or customer detail.
2. The SQL answers exactly the question, uses only logical relations, and each
   step runs as one query. Windows are half-open and complete days.
3. Revenue is named as `completed_item_sales` with its definition; other
   readings (gross, net, shipped) are labelled as such or exploratory.
4. Every figure traces to the fixture and is labelled illustrative; no live or
   production numbers appear.
5. The report states definition, window, scope and currency status.
6. Assumptions and limits are honest: current statuses, partial periods, small
   groups, overlapping order counts, scope limits.
7. Contributors are not presented as causes; hypotheses are marked as such.
8. Customers appear only as opaque references or aggregates; demographics are not
   called anonymous; no exact ages or fine location.
9. Nothing claims author experience or presents the seed as a historical record.
10. The method would not mislead on a different period, population or scope.

Current status: the development reviewer identity `demo-b` published revision 1
on the strength of the automated checks and this checklist as written. A named
human reviewer has not yet signed off; do that before treating the seeds as
reviewed knowledge in any shared environment.

## Held-out cases

Evaluation questions and retrieval cases must stay separate from these seeds. The
ten questions above are the seed side of that split: do not paraphrase them into
held-out evaluation cases or tune prompts and retrieval on held-out wording.
