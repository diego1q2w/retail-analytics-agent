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

## Proportion walkthrough (before/after, T26-F4)

`proportion_walkthrough.py` checks that the work matches the request: a
figure question ("What's the latest revenue of September?"), the follow-up
"And August?", a repeat ("What was September's revenue again?"), and a
separate report request ("Write a short report comparing August and September
revenue, with recommended actions."). Same harness as above (local backend,
offline DuckDB over the frozen extract, men's product scope, fresh sessions on
a throwaway PostgreSQL), one run each on 2026-10-09. "Before" is `32da67c`;
"after" is `ff6fcde` (which adds reusable schema context) plus this change.
Gemini `gemini-3.8-flash` answered every request in both; no fallback.
Reference revenue (completed item sales by order month, computed by the
script straight from the extract files): August 2025 33,665.86, September
2025 27,051.80.

| request | before | after |
| --- | --- | --- |
| September | 4 queries (month by year, data range, last 5 days, status mix), 9 requests, 55,764 tokens, 52.8 s; answer gave the month and the last day, a status breakdown, gross sales and suggested actions | 1 query (latest September found and totalled in one statement, status `Complete`), 3 requests, 23,053 tokens, 25.7 s; one sentence: 27,051.80, interpretation, period, definition, evidence id |
| "And August?" | 3 queries (month, status mix, daily), 6 requests, 46,327 tokens, 39.3 s; added last-day, status and comparison sections | 1 query, 2 requests, 14,616 tokens, 14.8 s; 33,665.86 stated the same way |
| September again | 0 queries, 3 requests (2 `fetch_evidence`), 27,177 tokens, 20.2 s; repeated the full breakdown | 0 queries, 1 request, 7,120 tokens, 6.0 s; the cited figure only |
| Report request | stopped on the token budget after 6 queries, 11 requests, 85,054 tokens, 53.9 s; no figure released | completed: 3 queries, 5 requests, 63,205 tokens, 97.1 s; saved report with cited findings (overall, categories), limitations and recommended actions |

The same change measured on `32da67c` alone (before the schema context) gave
1, 1 and 0 queries for the three scalar turns (5, 4 and 1 requests; 32,027,
24,892 and 6,385 tokens) and a completed report (4 queries, 74,895 tokens).

The monthly revenue figures in every answer and in the report match the
references; the report's other figures (categories, orders, statuses) were not
independently checked. One run per request on one day:
observations, not promises; the repeatable efficiency evaluation is separate.
Reproduce from the repository root (same requirements as the discovery
walkthrough): `python evaluation/real-model/proportion_walkthrough.py
[--only scalar report] [--show-answers]`. It prints each query's executed SQL
from the sanitized `query.compile` span content.

## Focused tool exposure (before/after, T26-F5)

`focus_walkthrough.py` records, at the provider boundary, which tool
definitions each model request is actually sent (names, characters of their
descriptions and JSON schemas), the instruction characters and the provider's
input/output tokens. Conversations: `scalar` ("What's the latest revenue of
September?", "And August?"), `complex` ("Why did revenue change between August
and September? Break it down by category and name the main contributors.") and
`mixed` (a September figure plus "Keep a copy of the result for the board so I
can find it again later", worded without "report"; then "Also show that
figure in the currency they use in Berlin, and remember that I prefer that
currency from now on."). Same harness as above (local backend, offline DuckDB
over the frozen extract, men's product scope, fresh sessions on a freshly
migrated throwaway PostgreSQL), one run each on 2026-10-09. "Before" is
`b136939`; "after" adds this change. Gemini `gemini-3.8-flash` answered every
request; no fallback.

Before, every request was sent all 17 business tools (13,668 schema
characters). After, an ordinary request is sent 9 (execute_analysis,
fetch_evidence, find_analysis_examples, list_relations, describe_relation and
four argument-free loaders): 5,036 characters, of which the loaders are about
1,200 (selection overhead on every request).

| run | before: requests / input tokens / queries | after | tools sent per request (after) |
| --- | --- | --- | --- |
| September | 2 / 12,922 / 1 | 3 / 12,948 / 1 | 9 |
| "And August?" | 2 / 13,437 / 1 | 2 / 8,881 / 1 | 9 |
| Why did revenue change | 5 / 41,522 / 4 | 6 / 38,333 / 5 | 9 |
| Figure + keep a copy | 4 / 27,840 / 2 | 3 / 15,827 / 1 | 9, then 13 after `load_report_tools` |
| Berlin currency + remember | 3 / 20,471 / 0 | 3 / 16,564 / 0 | 13 (currency and preference wording) |
| Total | 16 / 116,192 | 17 / 92,553 | |

Average input tokens per request fell from 7,262 to 5,444 (-25%). The
selection itself cost one loader call (one model step) in the "keep a copy"
run, and the loader definitions on every request; the before run of that turn
made a second query instead, so its request count still fell. All runs gave
the reference revenue (September 27,051.80, August 33,665.86); the "keep a
copy" turn saved a report in both; the Berlin turn remembered EUR and called
`convert_currency` in both. Before, it then asked whether to use the current
or a historical rate; after, the conversion refused for lack of a rate in the
offline setup and the answer stayed in the source currency, marked
incomplete. The `investigation.tool_focus` span recorded the initial set and
the change (`reports:loaded`, 9 to 13 tools).

One run per conversation on one day: observations, not promises. Run-to-run
variation is visible (the September turn took 3 requests after versus 2
before; the "why" run 5 queries versus 4). An earlier attempt of the after run
reused a database where the before run had already saved the EUR preference,
which exposed the currency tools from the first step; it was discarded and
rerun on a fresh database. Reproduce from the repository root (same
requirements as the discovery walkthrough): `python
evaluation/real-model/focus_walkthrough.py [--only scalar complex mixed]
[--show-answers]`.

## Conversation efficiency (T39-F3)

`efficiency.py` measures what ordinary analytical requests cost and whether
their answers are right, on the same harness as above (agent runtime, local
backend, offline DuckDB over the frozen extract, live provider chain, a
throwaway PostgreSQL). The suite, `efficiency/suite.json`, was fixed before
any run. It sets the scenarios, the repetitions, the targets and a spend
ceiling:

| scenario | turns (kind) | reps | declared targets |
| --- | --- | --- | --- |
| `scalar-ordinary` | "What's the latest revenue of September?" (cold) | 3 | <= 2 queries (the period may need data), <= 5 model requests |
| `scalar-typo` | "hw much revenu did we make in septmber 2025" (cold) | 3 | <= 1 query, <= 4 requests |
| `scalar-explicit` | September 2025, default definition, total only (cold) | 3 | <= 1 query, <= 4 requests |
| `followup-context` | September 2025 (cold), then "And August?" | 3 | each <= 1 query, <= 4 requests |
| `reuse-evidence` | July-September by month (cold), then "Which of those months was highest, and by how much did it beat the lowest?" | 3 | cold <= 1 query, <= 4 requests; reuse 0 queries, <= 2 requests |
| `clarify-missing-month` | "What was revenue in that month?", reply "September 2025" | 1 | asks first, 0 queries before asking, <= 1 query |
| `why-category-change` | why Q3 to Q4 2025 revenue changed, by category | 1 | correctness and completeness only |
| `report-concentration` | saved report on Q4 2025 customer concentration with actions | 1 | report saved with actions; correctness |

Every turn must also complete (a partial, cancelled or failed run has not
answered) and must not ask a question it was not expected to ask. These are
evaluation targets, not production limits. They are not changed after seeing
results: a target that proves wrong is flagged here instead.

**Spend ceiling**, declared before the run, per suite run: 3,000,000 model
tokens and 500 model attempts. The runner refuses to start a conversation
whose worst case (per-run budget: 100,000 tokens, 20 requests) could cross
the ceiling. The full suite's worst case is 24 runs = 2.4M tokens and 480
attempts.

**Correctness.** Expected figures are taken at run time from the frozen
extract's independent reference values (`../realdata/expected.json`, two
SQL routes), never from the agent. For example, September 2025 revenue for the
women's scope is `monthly_trend.month_3_revenue`. Each figure is checked in
the released text ("figures right") and, separately, in the evidence the run
produced or reused. Reuse turns cite earlier runs' evidence, and a derived
difference has no evidence cell, so the evidence match is reported but not
required. The checks also record period and definition words in the text,
SQL fragments (status filter, period) in the executed logical SQL and its
bound parameters, and whether the cited evidence IDs exist in the session.
A reviewer reads the transcripts (released text, clarification questions and
every query's SQL) as well: citations and nonempty output alone do not show
that an answer is right.

**Recorded per run:** run/session/executive IDs, code revision, provider and
model of every model attempt (failed ones and fallbacks included), input and
output tokens, every query attempt (succeeded, rejected before the warehouse,
or failed) with its SQL, tool sequence,
context restarts and their causes, admission decision, active and wall-clock
seconds, and the targets met. Every repetition is reported, failures
included. Results contain no thought text or secrets. The data is the
pseudonymized frozen extract. MLflow trace IDs are not recorded: these runs
use the in-process telemetry recorder. The run IDs identify each run.

### Baseline

Before the agent-instruction, schema-context and focused-tool changes. Code
`12373a0` (the runtime as committed; only this suite's evaluation files were
uncommitted), 2026-10-09 11:14-11:31 UTC. Every request was served by Gemini
`gemini-3.8-flash`, with no failed attempts, no fallback and no context
restarts. Used 1,013,048 tokens and 159 attempts over 24 runs (within the
ceiling). Results: [`efficiency/results/baseline.md`](efficiency/results/baseline.md),
[JSON](efficiency/results/baseline.json),
[transcripts](efficiency/results/transcripts/baseline.md).

| turn | targets met | figures right | queries | requests | input tokens (median, range) | active s |
| --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 0/3 | 3/3 | 4 (4-5) | 9 (7-11) | 50,593 (36,956-67,570) | 54 |
| scalar-typo | 0/3 | 0/3 | 0 | 0 | 0 | 0 |
| scalar-explicit | 0/3 | 3/3 | 1 | 7 (6-7) | 35,009 (29,288-36,053) | 30 |
| followup-context, cold | 0/3 | 3/3 | 2 | 8 (7-8) | 40,672 (35,748-41,875) | 44 |
| followup-context, "And August?" | 0/3 | 3/3 | 2 | 6 | 38,615 (38,477-40,749) | 39 |
| reuse-evidence, cold | 0/3 | 3/3 | 5 | 11 (10-11) | 60,405 (58,176-60,674) | 66 |
| reuse-evidence, reuse | 3/3 | 3/3 | 0 | 1 | 5,716 (5,693-5,963) | 16 |
| clarify-missing-month | 0/1 | 1/1 | 3 | 15 | 80,096 | 70 |
| why-category-change | 0/1 (partial) | 0/1 | 6 (+1 failed) | 11 | 81,172 | 83 |
| report-concentration | 1/1 | 1/1 | 5 (+1 failed) | 10 | 75,240 | 96 |

What the transcripts show:

- **Ordinary September question:** the agent resolved "latest September" to
  September 2025 from the data and stated 25,105.97 every time. It also
  answered the latest single day and added an unrequested status breakdown
  (4-5 queries). This is the over-work seen in the earlier live probe. One
  answer put "1,261 orders" next to 469 completed items, which looks like an
  all-status order count attached to completed revenue (reviewer note).
- **Typos:** request admission asked "What would you like to analyze?"
  before any model call, all three times. The suite does not answer
  unexpected questions, so the runs were cancelled and scored as misses.
- **Explicit scalar:** one query every time, but 6-7 model requests, spent
  on schema discovery, example search and preference inspection.
- **Follow-up and reuse:** "And August?" ran two queries. The reuse question
  needed no query and one model request, and stated August and the
  2,523.34 difference correctly.
- **Clarification:** the agent asked one focused question before any query.
  After the reply it ran three queries (total, statuses, categories) for a
  one-number question.
- **Why question:** the right quarter and category evidence existed (Q3
  89,156.88, Q4 88,611.08 stated), but the run hit the token budget before
  writing an answer. The partial answer showed product-level rows instead of
  the category drivers, so it is incomplete.
- **Report:** saved, with recommended actions. Total, top-10 share and
  customer count all matched the references.

### Candidate (after the fixes) and comparison

Code `7ab8f32`, a pre-squash commit of this task: the runtime of main at `1ea43de` plus this suite's evaluation files. Since the baseline, main
gained content capture, approved schema context reused across follow-ups,
proportional answers with the remaining token budget shown to the model,
focused tool exposure, a report currency rule, normal conclusion near the
budget, and typo-tolerant request admission. Run 2026-10-09 12:07-12:16 UTC
with the same suite, the same scoring (v2) and the same targets. Gemini
`gemini-3.8-flash` answered every request, with no failed attempts, no
fallback and no context restarts. Used 360,876 tokens and 63 attempts over
24 runs, within the ceiling. Results:
[`efficiency/results/candidate.md`](efficiency/results/candidate.md),
[JSON](efficiency/results/candidate.json),
[transcripts](efficiency/results/transcripts/candidate.md).

| turn | targets met before -> after | figures right | queries | requests | input tokens (median) | active s |
| --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 0/3 -> 3/3 | 3/3 -> 3/3 | 4 -> 1 (1-2) | 9 -> 3 (3-4) | 50,593 -> 13,915 (-72%) | 54 -> 23 |
| scalar-typo | 0/3 -> 3/3 | 0/3 -> 3/3 | 0 (stopped) -> 1 | 0 -> 2 | stopped -> 8,375 | - -> 13 |
| scalar-explicit | 0/3 -> 3/3 | 3/3 -> 3/3 | 1 -> 1 | 7 -> 2 | 35,009 -> 8,331 (-76%) | 30 -> 12 |
| followup-context, cold | 0/3 -> 3/3 | 3/3 -> 3/3 | 2 -> 1 | 8 -> 2 | 40,672 -> 8,355 (-79%) | 44 -> 12 |
| followup-context, "And August?" | 0/3 -> 3/3 | 3/3 -> 3/3 | 2 -> 1 | 6 -> 2 | 38,615 -> 9,010 (-77%) | 39 -> 12 |
| reuse-evidence, cold | 0/3 -> **0/3** | 3/3 -> 3/3 | 5 -> 3 (2-4) | 11 -> 4 (3-6) | 60,405 -> 18,681 (-69%) | 66 -> 27 |
| reuse-evidence, reuse | 3/3 -> 3/3 | 3/3 -> 3/3 | 0 -> 0 | 1 -> 1 | 5,716 -> 4,505 (-21%) | 16 -> 12 |
| clarify-missing-month | 0/1 -> 1/1 | 1/1 -> 1/1 | 3 -> 1 | 15 -> 3 | 80,096 -> 12,246 (-85%) | 70 -> 25 |
| why-category-change | 0/1 -> 1/1 | 0/1 -> 1/1 | 6 -> 3 | 11 -> 4 | 81,172 -> 25,227 (-69%) | 83 -> 64 |
| report-concentration | 1/1 -> 1/1 | 1/1 -> 1/1 | 5 -> 4 | 10 -> 6 | 75,240 -> 46,258 (-39%) | 96 -> 70 |

Per-repetition input tokens for the matched scalar turns (baseline ->
candidate): ordinary 50,593 / 36,956 / 67,570 -> 13,915 / 13,318 / 19,551;
explicit 29,288 / 36,053 / 35,009 -> 8,449 / 8,311 / 8,331. The typo turns
have no baseline token figure to compare: admission stopped them before any
model call, which never counts as efficient.

What the transcripts show:

- **Ordinary September question:** each answer states 25,105.97 for
  September 2025, the definition (completed items) and the period, with one
  citation, and no unrequested day or status breakdown. In every repetition
  the model's first query was refused by the compiler (`UNSUPPORTED_SQL`).
  It then resolved the latest September and the amount in one query (two in
  repetition 3). A refused query still counts as a failed query.
- **Typos:** admitted. The amount and the period are correct, in one query
  and two model requests.
- **Explicit scalar and follow-up:** one query and two model requests each.
  "And August?" kept the period from context and answered 25,291.09.
- **Miss: reuse-evidence, cold turn.** "Show me monthly revenue for July,
  August and September 2025" took 2-4 queries against the declared target of
  1, and 6 requests in one repetition (target 4). The figures were correct
  each time. The extra queries were not needed for the request. Some of them
  succeeded without creating stored evidence, so this harness has no SQL for
  them (see limitations). The target stays as declared; this is a miss.
- **Reuse turn:** no query, one request, correct highest month and difference.
- **Clarification:** one focused question before any query. After the reply,
  one query and the right amount.
- **Why question:** completed, with all seven reference figures stated: Q3
  89,156.88, Q4 88,611.08, change -545.80, Tops & Tees +2,674.82 as the
  largest gain and Sweaters -2,169.10 as the largest loss. It used category
  evidence and explained volume against price.
- **Report:** saved with recommended actions. Total, top-10 share and
  customer count match the references.

One run per complex scenario and three per scalar scenario: these are
observations, not guarantees. Latency fell in every matched turn, but no
latency threshold is derived from it.

### Restricted-join guidance check (T39-F5)

The refused first queries above were all one shape: a CTE that found the
latest year (or latest date) joined to `sales_items`. The compiler only joins
approved relations on declared joins, so it refused them (`unsupported_join`)
and the model rewrote the query. The fix is guidance only (the grammar was not
widened): the `execute_analysis` description, the agent instructions and the
compiler's correction message now state the join rule and show one
compiler-tested example that filters the month and matches the year with a
scalar subquery.

The records now keep every `execute_analysis` attempt. An attempt is
`rejected` when the compiler or input validation refused it before any
warehouse job; it is counted apart from successful and failed warehouse
queries (`queries ok/rejected/failed`), with the SQL the model wrote and the
reason code. Targets and scoring are unchanged.

Bounded rerun, code `b318902` (this task's commit before these results were added; same code), 2026-10-09 12:40-12:43 UTC, the same suite
scenarios `scalar-ordinary` and `reuse-evidence` (3 repetitions each), Gemini
`gemini-3.8-flash` on every request, 84,947 tokens and 15 attempts over 9
runs. Results: [`efficiency/results/t39f5-candidate.md`](efficiency/results/t39f5-candidate.md),
[JSON](efficiency/results/t39f5-candidate.json),
[transcripts](efficiency/results/transcripts/t39f5-candidate.md).

| turn | first query accepted | queries ok/rejected | requests | input tokens (median) | active s (median) |
| --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 0/3 -> 3/3 | 1/1 per rep -> 1/0 | 3 -> 2 | 13,915 -> 9,049 | 23 -> 14 |
| reuse-evidence, cold | 3/3 -> 3/3 | 3 (2-4)/0 -> 1/0 | 4 -> 2 | 18,681 -> 9,318 | 27 -> 16 |
| reuse-evidence, reuse | no query | 0 -> 0 | 1 -> 1 | 4,505 -> 4,850 | 12 -> 15 |

Every ordinary answer stated 25,105.97 for September 2025 (2025-09-01 to
2025-09-30), completed item sales, source currency not verified, with the
documented scalar-subquery pattern as the first and only query. The monthly
turns answered all three months correctly in one query, so this rerun has no
extra queries to classify; the earlier run's extra queries cannot be
classified after the fact. Nine runs on one day: observations, not a
guarantee that the model always writes supported SQL.

### Live smoke: HTTP API and real BigQuery

`live_smoke.py` ran once against the shipped local default: a live-mode API
(local execution) on a throwaway PostgreSQL and MLflow, real BigQuery
(`bigquery-public-data.thelook_ecommerce`), the local administrator's token
(every product), code `7ab8f32` (runtime of `1ea43de`), 2026-10-09 12:10 UTC. It asked "What's the
latest revenue of September?" and then "And August?". The expected amounts
were computed right after the conversation by the script's own BigQuery SQL
(completed items, `orders.created_at`), independently of the agent. Results:
[`efficiency/results/live-smoke.json`](efficiency/results/live-smoke.json),
[transcript](efficiency/results/transcripts/live-smoke.md).

| turn | run | answer | reference | queries | requests | tokens in/out | client s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| September | `run_55f0263f3b0c8ee47b87096cb778bf21` | 141,190.75 (September 2026) | 141,190.75 | 1 | 2 | 8,519 / 1,963 | 27.2 |
| August | `run_fb6b8459207f6ba20adeb0a85e36b2e4` | 110,644.81 (August 2026) | 110,644.81 | 1 | 2 | 8,961 / 1,152 | 21.1 |

Both runs were answered by Gemini `gemini-3.8-flash`, with no restarts, one
BigQuery job each (20 MiB billed) and status completed. The MLflow traces
(`tr-1d58bac503e79b0909738a6c134d79b9`, `tr-2239639bb4cca9d80b342fa609799575`)
show the sanitized prompt on every model attempt, the tool arguments, and
the model's SQL next to the executed SQL. In the executed SQL the scope
filter value is withheld (`@[withheld]`). The SQL resolves "latest
September" in the same query that computes its amount, and the August query
uses the period from context. The earlier live probe of the same two
requests on code `419bb9e` took 4 queries / 11 calls / 69,353 tokens and
2 queries / 7 calls / 58,770 tokens (historical single observations, not a
controlled baseline). The amounts are identical. This is one bounded
conversation, not a measurement of variance.

The scoring was changed once after this run and before any comparison. The
"completed" and "no unexpected question" targets were added, because the
cancelled typo runs had counted as meeting their query limits. "Figures right"
now uses the released text only. The saved baseline was rescored from its
recorded data (`--rescore`). Nothing was rerun, and no declared limit
changed.

Reproduce from the repository root (`.env` with the provider keys; a
migrated PostgreSQL with no local-execution API attached):
`python evaluation/real-model/efficiency.py --label <name> [--baseline
evaluation/real-model/efficiency/results/baseline.json] [--only <scenario>...]`.
`--rescore` recomputes the targets of a saved run without running anything.
The live smoke needs a live-mode API on a throwaway database and a token
file: `python evaluation/real-model/live_smoke.py --api <url> --token-file
<file> --revision <sha>` (`--rescan` re-reads the answers and traces of a
saved smoke). The script never prints the token, and its reference month
pair is fixed in the script (`MONTHS`). Unit tests:
`pytest tests/unit/evaluation/test_efficiency.py`.

Limitations of the efficiency suite: it is small (8 scenarios; 3
repetitions only for scalar and reuse); one model and provider (no fallback
occurred, so fallback cost is unmeasured); the offline warehouse has no
BigQuery latency; figure checks show a number is stated, not that it is
stated in the right place (the transcripts are for that); the harness
records SQL only for queries that created evidence (the in-process recorder
keeps no content payloads), so a successful query without stored evidence
shows its outcome but not its SQL; targets are evaluation targets, not
production caps.

## Limitations

- Ten conversations, one run each, one day: no variance estimate, and no
  quality threshold is derived from them.
- Text checks show that a figure is stated, not that it is stated in the right
  place or with the right meaning. That is what the pending human review is for.
- No judge ran. The 11 judge-blocked scenarios remain unvalidated.
- The run used the code at `e8b5794` plus this task's changes. Later commits on
  main (T38-F1 answer withholding, T38-F2 Golden-example masking, T41
  defaults) were not part of the run.
