# Observability: traces, metrics and dashboards

Local only (see the README for the Compose stack). Production hosting needs its own authentication, network restrictions, retention and backup design.

Traces contain the sanitized conversation of each investigation (see [Interaction content](#interaction-content)). Masking removes direct personal data and secrets, but the captured prompts, SQL and result previews are still the company's analytical data: the local MLflow listens on 127.0.0.1 only and has no user accounts, so treat it as operator-only. It does not enforce the product's executive or product-scope permissions; a production trace store needs operator-only access behind the company identity provider, its own retention, and the same data-processing terms as the warehouse.

## Switching it on

`TELEMETRY_ENABLED` is `true` by default (settings and `.env.example`); `./scripts/bootstrap.sh` and `./scripts/dev.sh` start MLflow, Prometheus and Grafana and print their URLs, unless `--no-telemetry` is given or the env file says `false` (an explicit `false` is never overwritten). Tests force it off, so `./scripts/check.sh` makes no network calls. With the services down, exports are dropped (bounded queue, short timeout, breaker) and runs are unaffected. Endpoints and timings:

| Setting | Default |
| --- | --- |
| `TELEMETRY_TRACES_ENDPOINT` | `http://127.0.0.1:55500/v1/traces` (MLflow, protobuf only) |
| `TELEMETRY_METRICS_ENDPOINT` | `http://127.0.0.1:59090/api/v1/otlp/v1/metrics` (Prometheus OTLP receiver) |
| `TELEMETRY_EXPERIMENT_ID` | `0` (MLflow experiment) |
| `TELEMETRY_EXPORT_TIMEOUT_SECONDS` | `2` |
| `TELEMETRY_METRIC_INTERVAL_SECONDS` | `10` |
| `TELEMETRY_CAPTURE_CONTENT` | `true` (sanitized interaction content in traces; `false` keeps metadata only) |

## Correlation

The trace id of a run is derived from its run id (`application.telemetry.trace_id_for`), so the API, the Temporal workflow's activities, tool calls, query attempts and model attempts of one run land in one MLflow trace without passing trace context between processes. Spans of a run that have no live parent span of the same run hang from the run's root span, whose id is also derived from the run id; the root span (`investigation.run`, start time = run creation) is emitted when the run closes, never has a parent, and carries the final status, budget use and the answering provider. A local run executes detached from the request that started it, as a Temporal worker does, so both backends export one acyclic tree. A run that never closes has no root span yet.

Attributes you can search on: `run_id`, `session_id`, `operation_id` (the idempotency key of one tool execution; also in every progress event), `job_id` (BigQuery job), `attempt`. A user-visible event carries `run_id` and `operation_id`; `python -m retail_analytics.bootstrap.trace_lookup <run_id> --tree` shows the matching tool attempt with its sanitized error code and summary. The tree lists every span once, also for incomplete traces (spans whose parent was not exported are marked `[parent missing]`) and for malformed traces recorded before the parentage fix (a parent cycle is reported as a warning and its spans are marked `[in parent cycle]`).

HTTP request spans are separate small traces that carry `run_id` and `run_trace_id` when the route has a run in its path. The run-creating request is linked through the `run.accept` span inside the run's trace.

## What each span records

| Span | Attributes |
| --- | --- |
| `run.accept` | run, session, status, created |
| `run.admission` | request admission codes: `decision` (`proceed`/`clarify`/`decline`/`reset_topic`), `topic`, `reason` (which rule decided), `classifier_version`; never the request text |
| `user.input` | run, input id, kind (`steering`/`answer`), question id |
| `clarification.ask` | run, sequence, outcome (`asked`/`withheld`), withhold reason |
| `answer.release` | run, sequence, outcome (`released`/`withheld`/`superseded`), withhold reason |
| `tool.call` | capability, version, attempt, operation, outcome (`succeeded`/`empty`/`pending`/`unknown`/`failed`), error code, sanitized summary |
| `query.compile` | operation, attempt, outcome (`compiled`/`rejected`), rejection reason |
| `query.execute` | operation, attempt, outcome, error code and reason, job id, bytes processed/billed, cache hit, result size |
| `retrieval.search` | outcome (`hit`/`no_match`/`unavailable`), counts, top similarity and lexical coverage, example ids, `review_sample` (about 1 in 10) |
| `model.request` | one logical request: attempts, whether it fell back, `answered_by`, `answered_model` |
| `model.attempt` | `provider`, `model`, `attempt` (within that provider), `request_sequence` (across providers), `fallback_from`, `fallback_reason`, outcome, reason class, token counts |
| `investigation.run` | status, budget use per resource, answering provider and model, fallback origin and reason |
| `http.request` | route template, method, status code |

Reason classes: `rate_limited`, `server_error`, `timeout`, `connection`, `rejected` (401/403/404), `cooling_down` (primary skipped without a request), `budget`, `other`. The provider that produced the final answer travels in the model response metadata and is recorded when the answer is released; the agent itself does not see it. The model-switch events are not emitted into the user-visible run events (traces and metrics only).

## Metrics

All names start with `ra_`. Label keys are limited to `status, outcome, capability, error_code, reason, provider, model, from_provider, to_provider, reason_class, direction, route, method, cause_type, resource, kind`, with short code-like values; identifiers can never become label values.

Runs (`ra_runs_total`, `ra_run_seconds`, `ra_runs_started_total`), budgets (`ra_run_budget_use_ratio`, `ra_budget_stops_total`), model conversation restarts (`ra_context_restarts_total` with a `reason` cause code), tools (`ra_tool_calls_total`, `ra_tool_seconds`, `ra_tool_retries_total`), models (`ra_model_requests_total`, `ra_model_request_seconds`, `ra_model_tokens_total`, `ra_model_fallbacks_total`, `ra_final_answers_total`), queries (`ra_queries_total`, `ra_query_seconds`, `ra_query_bytes_total`, `ra_compiler_rejections_total` with `cause_type` = parser exception class for fail-closed rejections), gate (`ra_output_gate_withholds_total`), retrieval (`ra_retrievals_total`, `ra_retrieval_seconds`, `ra_retrieval_review_samples_total`), HTTP (`ra_http_requests_total`, `ra_http_request_seconds`) and exporter health (`ra_telemetry_dropped_total`). A blocked request or a valid empty result is counted as such, not as a service failure.

Each process pushes as its own `instance`; sum across instances in queries. A restart starts new counter series, which `increase()` and `rate()` handle.

## Interaction content

Open a trace in the MLflow UI (Traces tab of the experiment) and select a span: its **Inputs** and **Outputs** show what crossed that boundary, in order. They are MLflow's own span input/output fields (`mlflow.spanInputs`/`mlflow.spanOutputs`), so no SQL, custom CLI or other frontend is needed; the metadata attributes above stay beside them.

| Span | Inputs | Outputs |
| --- | --- | --- |
| `run.accept` | the user's request as submitted | |
| `user.input` | a steering message or the reply to a clarification | |
| `model.attempt` (one per attempt, retries and fallback included) | provider, model, attempt, the messages actually sent (system instructions with the assembled, permission-filtered context; user and earlier assistant messages; tool results), offered tool names | the assembled response (text and tool calls with arguments, including the final-answer or clarification output tool), token usage; for a failed attempt the error class, HTTP status, reason class and sanitized message |
| `tool.call` | tool call id, operation id, tool, attempt, validated arguments (or `rejected_arguments` when they failed the schema) | `model_visible_result`: exactly the compact result the model receives |
| `query.compile` | `generated_sql` (the model's SQL) and its parameters | `executed_sql` (compiled, scope-filtered statement sent to the warehouse), `normalized_sql`, analysis parameters, relations; or the rejection code, reason and message |
| `query.execute` | | `internal_result`: column names and at most 20 rows *after* the result-privacy boundary, row counts, truncation and masked-cell counts (the model sees only the tool's compact output) |
| `clarification.ask` | `model_draft`: the question the model proposed | `released_question` after the output gate, or the withhold reason |
| `answer.release` | `model_draft`: the answer text, cited evidence and completeness the model proposed | `released_answer` that passed the output gate (with source notes) and the final status, or the withhold reason and the notice the user saw |
| `investigation.run` (root) | the request as the model read it (original request, steering, clarification replies) | final status and the released answer (MLflow shows these as the trace's request and response) |

A streamed model response is recorded once, after it is assembled. Never captured: provider reasoning or thought text, thought signatures and other opaque provider replay data (counted as `omitted.provider_private_parts`), file or binary content, authorization parameter values and key material of compiled queries, request objects or dependency objects (only explicit structured payloads built at these boundaries are captured).

Each side carries `capture.<side>.chars`, `capture.<side>.redactions`, `capture.<side>.truncated` and, when something was left out, `capture.<side>.omitted` (reason codes). Bounds: 20,000 characters per string, 200 items per list or object, nesting depth 10, and 32,000 characters of text per side; anything cut ends with an explicit `[truncated: ...]` marker. A model request whose history exceeds the limit therefore shows its first messages and a marker for the rest; no separate artifact is stored. Content passed through the bounded span queue, so worst-case trace memory stays bounded.

Set `TELEMETRY_CAPTURE_CONTENT=false` to export metadata only: spans keep timings, ids, providers, token counts, outcomes and error classes, and carry `content_capture=disabled`. Old traces never gain content retroactively.

## Sanitization

Everything passes through `application.telemetry` before an exporter sees it. Attributes: keys naming prompts, SQL, rows, tokens, secrets, emails, text and similar are dropped; identifier attributes must be identifier-shaped; other strings that look like data dumps (quotes, braces, `=`) are dropped and the rest are masked with `redact_for_telemetry` and secret-shaped patterns (API keys, bearer tokens, JWTs) and truncated. Exceptions on spans are recorded by class name only. Metric labels never carry content.

Interaction content goes through `application.telemetry_payloads`:

- every string uses the output gate's detectors (`redact_for_telemetry`: emails, phones, street addresses, postal codes, cued person names, raw customer/order/item keys, encoded identifiers, opaque references) and then secret shapes, `key=value` credentials and long opaque letter-digit runs; replacements read `[withheld]` or `[redacted]`;
- values under secret-named keys (password, token, api key, authorization, cookie, credential, signature, thought, reasoning) and under direct-identifier columns (`email`, `first_name`, `last_name`, `street_address`, `postal_code`, `city`, `latitude`, `longitude`, `user_id`, `customer_id`, ...) are withheld whatever they contain;
- SQL loses its comments; literals compared with an identity column are withheld, and string literals that are not plainly dates, short codes or lower-case words are withheld (several capitalized words could be a name, so even a literal like a two-word country name is withheld); parameter values follow the same rule;
- a payload that cannot be sanitized is replaced by `[omitted: payload could not be sanitized]` and the analysis continues.

These detectors are defense in depth; the compiler and the result boundary keep direct identifiers out of model input and results in the first place. A name typed without any cue ("Maria Lopez bought the most") is not detectable in free text and would appear in a captured request as typed. Tests inject canaries into every capture surface (`tests/unit/telemetry/test_content_capture.py`, `tests/unit/telemetry/test_facade.py`) and check live MLflow, Prometheus and container logs (`tests/integration/test_telemetry_e2e.py`).

## Failure behavior

Spans go through a bounded batch queue and metrics through a periodic reader, both exported on background threads with a 2 s timeout. After a failed export the exporter skips sends for a cool-off period. Data that cannot be sent is dropped and counted in `ra_telemetry_dropped_total`; the application never waits for it, and shutdown flushes for at most the export timeout. Mutation audits are written in the PostgreSQL transaction and do not depend on this path.

## Dashboard

Grafana folder "Retail Analytics" holds "Agent overview" (`docker/grafana/dashboards/agent-overview.json`, provisioned): runs, latency, budget use, query bytes, provider and fallback rates, final-answer provider, gate withholds, compiler rejections by class, retrieval hit/no-match, tool failures and exporter drops. The retrieval panels show volume and a human-review sample count only: relevance precision needs human labels, none exist for live traffic, and none is computed. The dashboard's MLflow link assumes the default port.

## Not covered

No alert rules (thresholds and targets are undecided), no retention settings, no span-level sampling beyond the retrieval review mark, and no OpenTelemetry context propagation across HTTP calls to the providers or BigQuery (those attempts are spans created by this code).
