# Held-out reference conversations

Synthetic data and conversation cases for the evaluation runner. Everything
here is invented: products, customers, orders, and the placeholder names and
`example.invalid` emails that act as canaries for the privacy cases.

## Contents

- `fixture/` - fixture `heldout-fixture-1`: 8 products, 14 customers and 26
  orders (August to mid-October 2026) as plain JSON. It includes mixed-product
  orders, every item status, items stamped on the last second of August and the
  first second of September, a product with only a cancelled item, a product
  never ordered, and an order on the partial current day.
- `reference-sql/` - one checked query per data-derived scenario, written
  against the raw source tables. Each returns one row whose column names are the
  observation names in the manifest.
- `manifest.json` - 25 scenarios in three levels (deterministic scope, privacy
  and calculation cases; multi-turn conversations and reports; adversarial
  cases). Five executive scopes: two disjoint product sets, all products, one
  product, and an empty scope.
- `../splits.json` - the explicit identifiers of the held-out set and of the
  training material it must stay apart from.

## How the expected values are established

The manifest literals are checked by two routes that share no code with the
compiler or the agent: the reference SQL run in DuckDB, and plain Python over
the fixture rows (`tests/unit/evaluation/test_heldout_fixtures.py`). A third
check compares revenue with the metric reference semantics. A change to the
fixture or to any literal fails those tests until all routes agree again.
Bump `manifest_version` when scenarios or expected values change.

Status of review: the figures are reproduced by the routes above. A named human
reviewer has not yet signed them off.

## What a target reports

A target returns an observation per scenario (`TargetObservation`):

- numbers and strings by the names used in `expectations` (for example
  `revenue`, `either_customers`, `top_state_total`);
- behavior flags derived from evidence and tool calls, not from the wording of
  the answer: `pii_released`, `exact_age_released`, `raw_customer_id_released`,
  `out_of_scope_data_released`, `full_basket_data_released`,
  `raw_table_query_executed`, `entitlement_taken_from_message`, and the number
  of `released_rows`;
- report-element flags: `definition_disclosed`, `scope_disclosed`,
  `contributors_listed`, `unsupported_causal_claim` (expected false),
  `action_items_present`, `evidence_cited`, `partial_period_labeled`,
  `empty_result_explained`, `sample_size_caveated`,
  `correction_scoped_to_request`, `empty_scope_explained`, `opaque_refs_used`;
- the answer text, checked for the literal canaries (`not_contains`) and for
  the names of the true contributors (`contains`).

Judge dimensions (`report-quality-v1`) score the parts a literal check cannot:
definition disclosure, contributors versus causes, action items, evidence
support, limitations and schema accuracy.

## Running

Every scenario requires the capability `agent_runtime`. Without it a run is
`blocked` (exit code 3), never passed. Once the agent target exists:

```sh
retail-analytics-eval run --manifest evaluation/heldout/manifest.json \
  --target <package.module:factory> --capability agent_runtime \
  --out evaluation-results/heldout.json
```

## Separation from the Golden seeds

The Golden seed library and its fixture (`golden-seed-fixture/1`) are training
material. Held-out scenarios use different products, customers, dates and
questions; a test rejects any scenario whose question closely matches a seed
question, and any shared product or fixture identifier. Do not tune prompts,
retrieval or seeds on these scenarios.
