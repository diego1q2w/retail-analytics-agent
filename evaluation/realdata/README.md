# Real-data conversational benchmark

Seven conversations (state, product, trend, customer concentration, age-band
demographics, headline revenue, and a three-turn category report) over the
public `thelook_ecommerce` tables. Expected values are real, but they are scored
against a **frozen, sanitized extract**, not against the live dataset.

## Extract files are local-only

The four frozen CSV files have been removed from the current tree. Scripts,
provenance and historical evaluation results remain. Live setup and chat do not
need these files; extract-based evaluations require local regeneration:

```sh
python -m retail_analytics.bootstrap.realdata_benchmark extract --refresh
python -m retail_analytics.bootstrap.realdata_benchmark expected
python -m retail_analytics.bootstrap.realdata_benchmark manifest
python -m retail_analytics.bootstrap.realdata_benchmark verify
```

The live dataset changes, so this creates a new benchmark snapshot; it does not
reproduce the published historical scores. Follow the versioning instructions
below and keep generated CSV files uncommitted. Earlier Git commits still contain
the original extract; this removal does not resolve its redistribution terms.

## Why a frozen extract

The public dataset is reloaded daily and statuses are restated ("Complete" is a
current state), so a closed date window alone does not freeze the numbers, and a
timestamp is not a data version. The benchmark therefore pins data, not just
dates.

- `extract/` - the rows of every order created in the window
  **2025-07-01 to 2026-01-01 (end exclusive, UTC, `orders.created_at`)**, all
  statuses, copied once from BigQuery. Date basis is the order date; item
  timestamps are a different clock and are not used for order-period sales.
- `extract/extract-manifest.json` - provenance: source dataset and table
  metadata at extraction (row counts, last-modified), extraction time, query
  fingerprints, job ids, bytes processed/billed, per-file sha256 and the
  extract digest. The digest is part of the manifest version.
- `expected.json` - expected values, computed from the extract by the reference
  SQL in `reference-sql/` (two structurally different routes per question that
  must agree), tied to the extract digest and the spec digest.
- `manifest.json` - the conversation manifest, generated from `spec.json` and
  `expected.json` (numbers are never typed by hand). `fixture_ref` is the data
  version (`thelook-realdata-extract-1`); `manifest_version` ends with the
  first 8 hex characters of the extract digest. Scopes use
  `products:<lo>-<hi>` ranges (women 1-15989, men 15990-29120).
- `spec.json` - windows, scopes, queries with output tolerances, and scenarios.

Review status: the figures are reproduced by two SQL routes and the routes were
also proven against the synthetic held-out fixture, whose answers are known
independently (`tests/unit/evaluation/test_realdata_benchmark.py`). No compiler,
metric-catalog or agent code is involved. A named human reviewer has not signed
them off.

## Sanitization

Done inside BigQuery before any row reaches this machine: order, customer and
item identifiers become dense pseudonyms (`DENSE_RANK`; originals are never
selected), `users.age` becomes its 5-year band start (top-coded 90), and names,
e-mails, addresses, coordinates and traffic source are never selected. Only
`orders`, `order_items`, `users(id, age, state)` and `products(id, name,
category)` columns listed in `EXTRACT_COLUMNS` are allowed. `verify` and the
tests scan every artifact for personal-data columns and e-mail addresses.

## Commands

All via `python -m retail_analytics.bootstrap.realdata_benchmark` (needs the dev
dependency DuckDB for everything except `extract` and `drift`).

| command | what it does |
| --- | --- |
| `verify` | Offline gate: files match digests, expected values and manifest reproduce from the extract, privacy scan passes. |
| `expected`, `manifest` | Regenerate `expected.json` / `manifest.json` after a spec change. |
| `extract --refresh` | Take a NEW extract from live BigQuery (a new data version; rerun `expected` and `manifest`, bump `benchmark_version`). Refuses to overwrite without `--refresh`. |
| `drift` | Re-run the reference SQL on the live warehouse and write `evaluation-results/realdata-drift.json`. |

## Frozen versus live

Scoring uses the extract only. The drift report is a **separate** artifact about
the source: it never changes expected values, the manifest or any score, and a
drift is never reported as an agent failure. A target for these scenarios must
answer from the frozen extract (capability `frozen_extract_source`, to be
provided with the agent runtime target; the extract is loadable as the four
source tables with `DuckDbExtractEngine`). A run of the agent against the live
warehouse is only a live smoke test; compare it with these values only when the
latest drift report shows no drift, and report it separately.

```sh
python -m retail_analytics.bootstrap.realdata_benchmark verify
retail-analytics-eval run --manifest evaluation/realdata/manifest.json \
  --target <package.module:factory> --capability agent_runtime \
  --capability frozen_extract_source --out evaluation-results/realdata.json
```

Without those capabilities every scenario is `blocked`, never passed.

## Separation from training material

Scenario ids and tags, questions and expected names stay out of the Golden seed
library (a test checks wording overlap), and `evaluation/splits.json` lists the
scenario ids as held out. Do not tune prompts, retrieval or seeds on them.
