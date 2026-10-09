# Real-model evaluation (T37)

A small, fixed set of conversations run with the real model through the
shipped runtime, with every figure checked against independently computed
references. It measures what ten conversations did on one day. It is not a
quality threshold, and it does not support a production-quality claim.

- [`results/SUMMARY.md`](results/SUMMARY.md): table per conversation.
- [`results/results.json`](results/results.json): machine-readable results
  (identifiers, counts and expected figures; no answer text).
- [`results/transcripts/`](results/transcripts/): what each conversation
  released (answers and saved reports). The data is pseudonymized (frozen
  extract) or synthetic (held-out fixture).
- [`human-review/`](human-review/): rubric and verdict sheet for the human
  report review. **The review is pending**: no one has reviewed the reports yet.

## What ran

| | |
| --- | --- |
| Date | 2026-10-09 |
| Runtime | `agent_runtime:local`: the real investigation runtime on the default **local** execution backend (PostgreSQL only), the same model steps, tools, compiler, privacy boundary, evidence store, output gate and reports as the API |
| Data | offline DuckDB warehouse. Seven conversations use the frozen extract `thelook-realdata-extract-1` (digest `8ba6e7f4…`, checked before the run; a corrupt or mismatched extract blocks the run). Three use the synthetic held-out fixture `heldout-fixture-1` |
| Model | provider chain: `gemini-3.8-flash` (Interactions API) primary, `gpt-5-mini` fallback configured |
| Who answered | **Gemini answered all 10 conversations** (115 successful requests; 2 failed Gemini attempts were retried on Gemini; no request fell back to GPT). Attribution comes from the runtime's own telemetry spans, recorded per run |
| Code | `e8b5794` plus this task's changes (including the Gemini schema fix below) |
| Judges | none. Judge-scored dimensions stay unscored |

The selection was fixed before any run and was not changed or tuned after
seeing outcomes. It covers customer, product, time and demographic analysis,
follow-ups (two- and three-turn), two saved/summary reports with next steps,
a schema question, a scoped definition correction and a request for personal
data:

- frozen extract (T35, all 7): `rd-l1-revenue-q3-customers`,
  `rd-l1-state-top3-q4`, `rd-l1-product-top3-q4`, `rd-l1-age-band-spend-q4`,
  `rd-l2-monthly-trend-h2`, `rd-l2-customer-concentration-report`,
  `rd-l2-category-change-report`;
- held-out fixture (T33, 3 of 25): `ho-l1-source-schema-question`,
  `ho-l2-definition-correction`, `ho-l1-pii-names-emails`.

## Results

All 10 conversations were answered by Gemini, so every number below is a
Gemini number (no fallback runs to separate).

| measure | result |
| --- | --- |
| Expected numbers found in the released evidence (any column) | **41/41** |
| Expected numbers stated in the released answer or report | **40/41** |
| Expected labels stated (states, products, categories, age bands) | **16/16** |
| Privacy/scope flags raised | 1/10 conversations (heuristic false alarm, see below) |
| Strict named-observation checks (existing runner) | 29/80 |
| Runner statuses | 1 passed, 8 failed, 1 blocked (`judge_unavailable`) |
| Runs | 12 completed, 3 partial, none failed (15 runs) |
| Model requests per conversation | 4 to 25 (115 in total); 39 queries in total |
| Latency per conversation | 22 s to 269 s |

How to read this:

- **Figures were checked independently of the agent's labels.** The expected
  values come from the reference SQL (frozen extract: two structurally
  different DuckDB routes, T35; held-out fixture: SQL plus Python, T33). The
  check looks for each value in any cell of the released evidence and,
  separately, in the released text. It does not use definition stamps on the
  evidence: a stamp says which definition was in context, not that the SQL
  implemented it. A correct evidence row alone is not counted as a correct
  answer; the text check is reported separately.
- **The one number not stated** is `best_month_index` = 5 in
  `rd-l2-monthly-trend-h2`: the answer names November (the fifth month) in
  words, which a number check cannot see. Text matching allows display
  rounding up to 1% (for example 73.2k for 73,164.81) and a share written as a
  percent; signs are compared as magnitudes (the reviewer judges the direction).
- **Label matching treats en dashes as hyphens** ("65–69" = "65-69"). This was
  fixed after the run; the two age-band labels were re-scored from the saved
  transcripts (released text only). Before the fix that line read 14/16.
- **The strict runner checks (29/80) mostly fail on column names, not values.**
  They read values by output column name (`revenue`, `state_1`, ...), which
  the scripted plans use and a real model does not. The scenarios that
  "failed" did so on those names, and none of their figures were wrong. The runner result is kept
  as it is, not relabelled.
- **Judge-required scenarios stay unvalidated.** Four of the ten
  (`rd-l2-customer-concentration-report`, `rd-l2-category-change-report`,
  `ho-l1-source-schema-question`, `ho-l2-definition-correction`) need a judge;
  no judge ran, so their judged dimensions are unscored. The runner shows
  three of them as `failed` (strict name checks take precedence) and one as
  `blocked`. Report-element flags (definition disclosed, action items, ...)
  are text heuristics. Neither is a quality verdict. All 11 judge-blocked
  scenarios of the T33/T35 suites remain judge-blocked.
- **The privacy flag is a false alarm.** In `ho-l2-definition-correction` the
  `full_basket_data_released` heuristic fired on 150.00. That is the in-scope
  returned Aster Parka item, alone in its order. It happens to equal the total
  of two mixed orders (Birch Sneakers 95.00 + an out-of-scope 55.00 item). No
  out-of-scope item or total was released. No names, emails, exact ages or raw
  customer IDs were released in any conversation, and the personal-data
  request was answered with opaque references and age bands only.
- Partial runs: the personal-data request (the agent could not fulfil the
  names/emails part), and one turn each in the monthly-trend and
  category-change conversations.
- Only one of the two report conversations saved a report
  (`rd-l2-category-change-report`); the concentration "summary" was given as
  an answer with next steps, not a saved report.

## Gemini fix found by this evaluation

Before this task, **every live agent request to Gemini failed with HTTP 400
("Request contains an invalid argument") and fell back to GPT**. So earlier
live agent runs that were taken to be Gemini (T26's live smoke, for example)
were in fact answered by GPT. Single-prompt live tests still passed, because
they send no tools. The cause: the full tool catalog's `maxItems` bounds,
with `tool_choice: any`. This task's first trial runs reproduced it (all
attempts `google-interactions … failed`, answered by `openai`); those trial
runs are not part of the results. The adapter now leaves `maxItems` out of
the schema sent to Gemini (`provider_schema` in
`adapters/models/gemini_interactions.py`). The bound is still enforced: the
tool gateway validates every returned argument against the full input model,
and a unit test checks that an over-long list is rejected.

## Live data: drift only

`python -m retail_analytics.bootstrap.realdata_benchmark drift` (2026-10-09
09:27 UTC) re-ran the seven reference queries on live BigQuery
(`bigquery-public-data.thelook_ecommerce`). **All seven differ from the frozen
extract** (for example Q3 2025 revenue 71,400.72 live vs 73,164.81 frozen).
The public source has changed since the extract was taken. This is a statement about the
source, not about the agent: scores above use the frozen extract only, and
live answers cannot be compared with these expected values. No agent
conversation was run against the live warehouse in this task.

## Reference sign-off

The expected figures were computed by two independent SQL routes (T33, T35).
No named person has signed off the business definitions or the expected
values yet; that review is still pending.

## Reproduce

Needs the local stack from `./scripts/bootstrap.sh` (PostgreSQL migrated to
head; `APP_DATABASE_URL` points at it) and a `.env` with `GEMINI_API_KEY`
(optionally `OPENAI_API_KEY` for the fallback). From the repository root:

```sh
python -m retail_analytics.bootstrap.realdata_benchmark verify
python evaluation/real-model/run_real_model.py --out evaluation/real-model/results
```

`--select suite:scenario ...` runs a subset (default: the ten above). No API
process may be running on the same database with local execution (the
evaluation takes the local-execution lock and otherwise reports every case as
blocked). It costs about 115 model requests and no BigQuery bytes (the warehouse is
offline DuckDB). The drift check above is separate:
`python -m retail_analytics.bootstrap.realdata_benchmark drift` (BigQuery
credentials; writes `evaluation-results/realdata-drift.json`, gitignored).

Unit tests for the attribution and figure checks:
`pytest tests/unit/evaluation/test_real_model.py tests/unit/models/test_gemini_interactions.py`.

## Discovery walkthrough (before/after, T39-F1)

`discovery_walkthrough.py` measures a first conversation: "What data do you
have, and what questions can you help me answer?" followed by "orders", plus
an explicit "How many orders are there, and what date range does the data
cover?". Same harness as above (local backend, offline DuckDB over the frozen
extract, fresh evaluation sessions on a throwaway PostgreSQL), one run each on
2026-10-09. "Before" is the code before this change; "after" includes it.
Gemini `gemini-3.8-flash` answered every request in both; no fallback.

| conversation | before | after |
| --- | --- | --- |
| Opening question | clarification asked before any model call ("What would you like to analyze?"); "orders" answered it | admitted as data discovery, answered directly |
| Opening + "orders": tools | list_relations, describe_relation x4, execute_analysis x5 (4 queries) | turn 1: list_relations, describe_relation x4; turn 2: describe_relation, inspect_preferences (0 queries) |
| Model requests / tokens (in+out) | 8 / 44,115 + 5,701 | 3 / 14,516 + 1,515, then 3 / 15,709 + 1,830 |
| Active time | 59.5 s (user wait not counted; the harness answers at once) | 18.9 s + 20.3 s |
| Answer | schema overview plus unrequested cancellation/return counts, customer count and coverage period | four subjects, supported periods, four example questions; "orders" narrowed to the orders fields and example questions, then asked what to measure. No figures, no saved-report listing |
| Explicit count/date-range question | not run | list_relations, describe_relation x2, find_analysis_examples, execute_analysis x2, inspect_preferences: 2 queries, 8 requests, 40,429 + 2,432 tokens, 37.6 s; order count and date range cited from evidence |

One run per conversation on one day: these are observations, not latency or
token promises. The model still chooses its tools (for example it described
all four relations for the overview). Reproduce from the repository root
(needs a migrated PostgreSQL with no local-execution API attached, and the
`.env` keys): `python evaluation/real-model/discovery_walkthrough.py
[--only overview profiling followup] [--show-answers]`.

## Approved schema context (before/after, T07-F1)

Each model request now carries the executive's approved schema (relations,
fields, joins, approved metrics) and states the effective preferences, so
ordinary questions need no discovery calls. The `followup` conversation asks
"What was our total revenue in October 2025? Just the total, please." then
"And November?". Same harness, one run each on 2026-10-09, code `32da67c`
(before) and with this change (after); Gemini `gemini-3.8-flash` answered
every request, no fallback.

| run | before: discovery calls / requests / input tokens | after |
| --- | --- | --- |
| October total | 4 (list_relations, describe_relation x3) plus inspect_preferences, find_analysis_examples / 8 / 39,753 | 0 / 2 / 11,190 |
| "And November?" | 3 (list_relations, describe_relation x2) plus inspect_preferences / 5 / 27,453 | 0 / 2 / 12,359 |

Each run made one query in both. A repeat of the after run gave 0 discovery
calls, 2 requests and 11,131 / 12,122 input tokens, with totals of 28,671.06
(October) and 28,625.06 (November) cited from evidence, and the overview
conversation answered without tools (1 request each turn, about 5,500 input
tokens). One run per variant: observations, not promises. The discovery
tools stay available for deeper exploration.

## Limitations

- Ten conversations, one run each, one day: no variance estimate, and no
  quality threshold is derived from them.
- Text checks show that a figure is stated, not that it is stated in the right
  place or with the right meaning. That is what the pending human review is for.
- No judge ran. The 11 judge-blocked scenarios remain unvalidated.
- The run used the code at `e8b5794` plus this task's changes. Later commits on
  main (T38-F1 answer withholding, T38-F2 Golden-example masking, T41
  defaults) were not part of the run.
