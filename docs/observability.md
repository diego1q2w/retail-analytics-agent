# Observability: traces, metrics and dashboards

Local only (see the README for the Compose stack). Production hosting needs its own authentication, network restrictions, retention and backup design.

## Switching it on

`RETAIL_ANALYTICS_TELEMETRY_ENABLED=true` in the API and worker environment (default `false`; `./scripts/bootstrap.sh --telemetry` sets it in a new env file). Endpoints and timings:

| Setting | Default |
| --- | --- |
| `RETAIL_ANALYTICS_TELEMETRY_TRACES_ENDPOINT` | `http://127.0.0.1:55500/v1/traces` (MLflow, protobuf only) |
| `RETAIL_ANALYTICS_TELEMETRY_METRICS_ENDPOINT` | `http://127.0.0.1:59090/api/v1/otlp/v1/metrics` (Prometheus OTLP receiver) |
| `RETAIL_ANALYTICS_TELEMETRY_EXPERIMENT_ID` | `0` (MLflow experiment) |
| `RETAIL_ANALYTICS_TELEMETRY_EXPORT_TIMEOUT_SECONDS` | `2` |
| `RETAIL_ANALYTICS_TELEMETRY_METRIC_INTERVAL_SECONDS` | `10` |

## Correlation

The trace id of a run is derived from its run id (`application.telemetry.trace_id_for`), so the API, the Temporal workflow's activities, tool calls, query attempts and model attempts of one run land in one MLflow trace without passing trace context between processes. Spans of a run that have no active parent hang from the run's root span, whose id is also derived from the run id; the root span (`investigation.run`, start time = run creation) is emitted when the run closes, with the final status, budget use and the answering provider. A run that never closes has no root span yet.

Attributes you can search on: `run_id`, `session_id`, `operation_id` (the idempotency key of one tool execution; also in every progress event), `job_id` (BigQuery job), `attempt`. A user-visible event carries `run_id` and `operation_id`; `python -m retail_analytics.bootstrap.trace_lookup <run_id> --tree` shows the matching tool attempt with its sanitized error code and summary.

HTTP request spans are separate small traces that carry `run_id` and `run_trace_id` when the route has a run in its path. The run-creating request is linked through the `run.accept` span inside the run's trace.

## What each span records

| Span | Attributes |
| --- | --- |
| `run.accept` | run, session, status, created |
| `tool.call` | capability, version, attempt, operation, outcome (`succeeded`/`empty`/`pending`/`unknown`/`failed`), error code, sanitized summary |
| `query.execute` | operation, attempt, outcome, error code and reason, job id, bytes processed/billed, cache hit, result size (never SQL or values) |
| `retrieval.search` | outcome (`hit`/`no_match`/`unavailable`), counts, top similarity and lexical coverage, example ids, `review_sample` (about 1 in 10) |
| `model.request` | one logical request: attempts, whether it fell back, `answered_by`, `answered_model` |
| `model.attempt` | `provider`, `model`, `attempt` (within that provider), `request_sequence` (across providers), `fallback_from`, `fallback_reason`, outcome, reason class, token counts |
| `investigation.run` | status, budget use per resource, answering provider and model, fallback origin and reason |
| `http.request` | route template, method, status code |

Reason classes: `rate_limited`, `server_error`, `timeout`, `connection`, `rejected` (401/403/404), `cooling_down` (primary skipped without a request), `budget`, `other`. The provider that produced the final answer travels in the model response metadata and is recorded when the answer is released; the agent itself does not see it. The model-switch events are not emitted into the user-visible run events (traces and metrics only).

## Metrics

All names start with `ra_`. Label keys are limited to `status, outcome, capability, error_code, reason, provider, model, from_provider, to_provider, reason_class, direction, route, method, cause_type, resource, kind`, with short code-like values; identifiers can never become label values.

Runs (`ra_runs_total`, `ra_run_seconds`, `ra_runs_started_total`), budgets (`ra_run_budget_use_ratio`, `ra_budget_stops_total`), tools (`ra_tool_calls_total`, `ra_tool_seconds`, `ra_tool_retries_total`), models (`ra_model_requests_total`, `ra_model_request_seconds`, `ra_model_tokens_total`, `ra_model_fallbacks_total`, `ra_final_answers_total`), queries (`ra_queries_total`, `ra_query_seconds`, `ra_query_bytes_total`, `ra_compiler_rejections_total` with `cause_type` = parser exception class for fail-closed rejections), gate (`ra_output_gate_withholds_total`), retrieval (`ra_retrievals_total`, `ra_retrieval_seconds`, `ra_retrieval_review_samples_total`), HTTP (`ra_http_requests_total`, `ra_http_request_seconds`) and exporter health (`ra_telemetry_dropped_total`). A blocked request or a valid empty result is counted as such, not as a service failure.

Each process pushes as its own `instance`; sum across instances in queries. A restart starts new counter series, which `increase()` and `rate()` handle.

## Sanitization

Everything passes through `application.telemetry` before an exporter sees it: attribute keys naming prompts, SQL, rows, tokens, secrets, emails, text and similar are dropped; identifier attributes must be identifier-shaped; other strings that look like data dumps (quotes, braces, `=`) are dropped and the rest are masked with `redact_for_telemetry` and secret-shaped patterns (API keys, bearer tokens, JWTs) and truncated. Exceptions are recorded by class name only. Free text is for application-authored codes and messages: personal names inside arbitrary prose cannot be detected, so callers never pass user, model or row text. Tests inject canaries (`tests/unit/telemetry/test_facade.py`) and check live MLflow, Prometheus and container logs (`tests/integration/test_telemetry_e2e.py`).

## Failure behavior

Spans go through a bounded batch queue and metrics through a periodic reader, both exported on background threads with a 2 s timeout. After a failed export the exporter skips sends for a cool-off period. Data that cannot be sent is dropped and counted in `ra_telemetry_dropped_total`; the application never waits for it, and shutdown flushes for at most the export timeout. Mutation audits are written in the PostgreSQL transaction and do not depend on this path.

## Dashboard

Grafana folder "Retail Analytics" holds "Agent overview" (`docker/grafana/dashboards/agent-overview.json`, provisioned): runs, latency, budget use, query bytes, provider and fallback rates, final-answer provider, gate withholds, compiler rejections by class, retrieval hit/no-match, tool failures and exporter drops. The retrieval panels show volume and a human-review sample count only: relevance precision needs human labels, none exist for live traffic, and none is computed. The dashboard's MLflow link assumes the default port.

## Not covered

No alert rules (thresholds and targets are undecided), no retention settings, no span-level sampling beyond the retrieval review mark, and no OpenTelemetry context propagation across HTTP calls to the providers or BigQuery (those attempts are spans created by this code).
