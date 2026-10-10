# Development and local operations

Reference for people changing or operating the project locally. The short reviewer path is in the [README](../README.md).

## What bootstrap and dev.sh do

`./scripts/bootstrap.sh` is idempotent and does everything needed for a working, seeded local environment for live analysis once external credentials are supplied:

1. creates `.venv` and installs the pinned dependencies if no virtualenv is active;
2. creates `.env` from `.env.example`, or only adds the setup keys an existing `.env` lacks. Ordinary defaults (run limits, models, embedding provider, mode) are not written, only documented as `# NAME=value` comments, so a default that changes later applies to you. It never overwrites or reorders a value, generates local-only secrets (`AUTH_SIGNING_KEY`, `REFERENCE_KEY`, and the database passwords for a new Compose volume) with `secrets`, and fills the connection defaults. After the steps it prints the effective execution mode, embedding provider and run deadline with their source (default, env file or environment) and flags values equal to an earlier default (for example `RUN_ACTIVE_SECONDS=600`) as possible stale overrides, suggesting their removal and never editing them. It prints `<generated>`, `<kept>`, `<default>` or `<missing: action>` per key, never a value;
3. checks Docker and Compose, starts PostgreSQL and waits until it is healthy (Temporal only when [selected](#temporal-execution-opt-in));
4. starts the local telemetry stack (MLflow, Prometheus, Grafana; on by default, `--no-telemetry` skips it);
5. runs `alembic upgrade head`;
6. provisions the local admin and the two restricted demo brand managers;
7. seeds the Golden knowledge library, generates embeddings and reads them back from PostgreSQL to verify persistence; bootstrap stops if warm-up fails or does not confirm stored embeddings;
8. validates the configuration and, when BigQuery and Gemini are configured, checks that access;
9. syncs the brand catalog for the brand managers: from BigQuery in live mode, from the synthetic fixture brands in fixture mode.

`./scripts/bootstrap.sh --list-steps` prints the same ordered list.

`./scripts/dev.sh` (or `python -m retail_analytics.bootstrap.dev_up`) starts the backend; call it with a dev token through the [CLI](cli.md) or the [HTTP and SSE API](http-api.md).

By default investigations run inside the API process (local execution): `dev.sh` needs only PostgreSQL, and no Temporal server or worker is started. A run keeps going when the CLI disconnects; stopping the API ends running investigations as interrupted (they are not resumed; send the request again). Durable Temporal execution is [opt-in](#temporal-execution-opt-in).

`dev.sh` makes sure PostgreSQL, the telemetry stack (MLflow, Prometheus, Grafana; skip with `--no-telemetry`) and the migrations are in place (the same bootstrap steps; unrelated containers are never touched), starts `retail-analytics-api` with the same environment file and `[api]`-prefixed logs, waits until `/healthz` answers for the selected execution backend, then prints the API URL, the Grafana and MLflow URLs and the command that issues a dev token (the token and secrets are never printed). Ctrl-C or SIGTERM stops what it started (SIGTERM, then SIGKILL after 10 s; with local execution after `LOCAL_SHUTDOWN_GRACE_SECONDS` + 5 s, so the API first ends its running investigations as interrupted) and exits 0; if a process exits on its own, the command exits 1 naming it. It refuses to start if the API port is taken by something else. Options: `--env-file FILE` (the same isolation as bootstrap: only that file is read, parent-shell setting variables are dropped), `--project NAME` (Compose project), `--execution-backend local|temporal` (this run only), `--no-services` (do not touch Docker; only check that the needed services are reachable), `--no-telemetry` (do not start the telemetry stack; `--telemetry` is accepted and does nothing), `--ready-timeout SECONDS`. It is a local-development convenience, not a supervisor.

External credentials (BigQuery project, Gemini key, optional OpenAI key) cannot be generated: they stay empty with a pointer to [docs/google-access.md](google-access.md), and explicit fixture mode works without them but does not analyze data. Add them to `.env` and rerun, or use `--interactive` to be asked (secrets use hidden input). Never regenerate a non-empty `REFERENCE_KEY`: rotating it invalidates every existing customer reference.

Options: MLflow, Prometheus and Grafana start by default and the next steps print their URLs; `--no-telemetry` skips them (and writes `TELEMETRY_ENABLED=false` when that key is new), `--telemetry` is accepted and does nothing; `true` is the default and is not written; an explicit `false` is never overwritten (the stack is then not started either); `--env-file FILE` works on another environment file (the Compose and every child command then use only its values; the repository's `.env` is never read or changed); `--project NAME`, `--postgres-port`, `--temporal-port` pick an isolated Compose project and free ports; `--execution-backend local|temporal` selects the backend for this run (written to the env file only for `temporal` and only when the key is new); `--env-only` only creates or completes the env file; `--list-steps` prints the ordered steps.

## Temporal execution (opt-in)

`EXECUTION_BACKEND` selects where investigations execute, independently of fixture/live mode: `local` (default) runs them in the API process over PostgreSQL; `temporal` runs them as durable Temporal workflows on `retail-analytics-worker`, which resume after a process restart. To use Temporal, set `EXECUTION_BACKEND=temporal` in `.env` (or pass `--execution-backend temporal` to `bootstrap.sh`/`dev.sh` for one run). Bootstrap and `dev.sh` then also start the Temporal server and its namespace, and `dev.sh` runs the worker next to the API with `[worker]`/`[api]` logs and waits for the worker's Temporal connection; `TEMPORAL_ADDRESS` is required (bootstrap fills the local default). With local execution `retail-analytics-worker` exits at once with an instruction.

Existing environment files: one without the setting runs local, even if it still has a Temporal address; nothing is added, and Temporal values, containers and volumes are kept. An explicit `temporal` is never changed. Runs are never moved between backends: if the other backend still has active investigations or queued requests, the API refuses to start, lists them and says how to finish or cancel them with their original backend. Details, guarantees and limits: [investigation runtime](investigation-runtime.md). The production design keeps Temporal with separately scaled workers; local execution is the single-process local topology.

## Local services (Docker Compose)

`compose.yaml` defines one PostgreSQL 17 server, Temporal 1.32 (only with Temporal execution) and the local telemetry stack described in [components](components.md#local-telemetry-mlflow-prometheus-grafana) (image versions pinned), all bound to loopback only. Requires Docker with Compose v2. Passwords are throwaway local defaults; override with `COMPOSE_APP_DB_PASSWORD`, `COMPOSE_TEMPORAL_DB_PASSWORD`, `COMPOSE_PG_ADMIN_PASSWORD`.

```sh
docker compose up -d --wait postgres               # project retail-analytics-local (local execution)
docker compose up -d --wait temporal               # Temporal execution only
docker compose run --rm temporal-namespace         #   and its namespace (7-day closed-history retention)
export APP_DATABASE_URL=postgresql+psycopg://retail_app:local-only-app@127.0.0.1:55442/retail_app
alembic upgrade head                               # application schema; safe to rerun
docker compose down                                # keeps the volume; add -v to delete data
```

| What | Value |
| --- | --- |
| PostgreSQL | `127.0.0.1:55442` (`COMPOSE_POSTGRES_PORT`) |
| Temporal frontend | `127.0.0.1:57233` (`COMPOSE_TEMPORAL_PORT`), namespace `default` (`COMPOSE_TEMPORAL_NAMESPACE`) |
| Roles / databases | `retail_app` owns `retail_app`; `temporal` owns `temporal` and `temporal_visibility`; `db_admin` is the superuser for administration only |
| Volume | `postgres-data` |

Temporal and the application never share a database or role: each role is the only one allowed to connect to its own databases, so the application cannot read Temporal's internal tables. Use `docker compose -p <name>` (and different ports) for a second isolated stack. Roles and databases are created on first start of an empty volume; the Temporal schema is applied by the one-shot `temporal-schema` service on every start.

Migrations live in `migrations/` (Alembic, configured by `alembic.ini`; the URL comes from `APP_DATABASE_URL` or `.env`). The baseline revision creates `app_meta`; revision `0002` adds sessions, messages, runs, tool executions (with BigQuery job detail) and the append-only execution and run event histories; revision `0003` adds executives and product entitlements; revision `0020` adds brand assignments and the product-brand catalog snapshot. Add new revisions with `alembic revision -m "..."` chained after the current head; a test keeps the history a single linear chain.

Application state is reached through the narrow ports in `retail_analytics.application.ports.persistence`, implemented with SQLAlchemy Core in `retail_analytics.adapters.postgres` and wired by `retail_analytics.bootstrap.persistence`. Retried writes are idempotent on application-generated keys (the operation ID for tool executions, the submission key for runs); reusing a key for different content raises a typed conflict. A session has at most one active run, enforced by a row lock and a partial unique index. Run events carry a gap-free per-run sequence for replay after a client's last received event ID.

Docker-dependent tests carry the `docker` marker and are excluded from `./scripts/check.sh`. Run them with `python -m pytest -m docker`; they start their own uniquely named Compose project on free ports and remove it afterwards.

## Development

Requires Python 3.12 (`requires-python = ">=3.12,<3.13"`, also in `.python-version`). [uv](https://docs.astral.sh/uv/) is recommended for creating the environment and regenerating the lock file; plain `pip` installs work too. Run commands from the repository root.

```sh
uv venv --python 3.12              # or: python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # package (editable) + pinned deps + dev tools
./scripts/check.sh                 # every check; must pass before each commit
```

## Checks

`./scripts/check.sh` runs, in order:

| Check | Command |
| --- | --- |
| Lint | `ruff check .` |
| Formatting | `ruff format --check .` (fix with `ruff format .`) |
| Types (strict) | `mypy` |
| Tests, including architecture | `python -m pytest` |

Architecture checks alone: `python -m pytest tests/architecture`. Tests run offline in fixture mode, and `tests/conftest.py` hides any local setting variables (and older `RETAIL_ANALYTICS_*`/`ANALYTICS_CLI_*` names) and `.env` from them. Tests that need real BigQuery or model credentials use the `live` marker and skip when credentials are absent; a skipped test is never reported as passing.

## Entry points

| Command | Module | Purpose |
| --- | --- | --- |
| `analytics` | `retail_analytics.bootstrap.cli` | CLI client and prototype UI; talks to the backend over HTTP only (`analytics chat`). See [CLI guide](cli.md) |
| `retail-analytics-api` | `retail_analytics.bootstrap.api` | Authenticated HTTP/SSE investigation API; needs PostgreSQL and the signing key, and runs the investigations itself with local execution (Temporal execution: also Temporal and the worker). See [HTTP and SSE API](http-api.md) |
| `retail-analytics-check-credentials` | `retail_analytics.bootstrap.check_credentials` | Verify BigQuery and Gemini access without printing secrets; see [Google access setup](google-access.md) |
| `retail-analytics-worker` | `retail_analytics.bootstrap.worker` | Temporal investigation worker, only with `EXECUTION_BACKEND=temporal` (fixture model, or the live Gemini/GPT chain); exits with status 3 otherwise |
| `retail-analytics-dev-access` | `retail_analytics.bootstrap.dev_access` | Development only: provision the local admin and the two restricted synthetic brand managers, issue local tokens, sync the brand catalog and assign/remove brands; see [authentication and entitlements](components.md#authentication-and-entitlements) |
| `retail-analytics-knowledge` | `retail_analytics.bootstrap.knowledge_admin` | Development only: submit, review and publish Golden examples; the local admin may publish their own, audited as self-published. See [local administration](local-admin.md) |

Live mode (`APP_MODE=live`) also requires `AUTH_SIGNING_KEY`, and the API requires it in every mode (no route skips authentication).

Backend entry points accept `--check-config`: validate settings, print them with secrets shown only as `<set>`/`<unset>`, and exit. Invalid configuration exits with status 2.

## Configuration

`./scripts/bootstrap.sh` creates `.env` for you (see the README quick start); to do it by hand, copy `.env.example` to `.env` (ignored by Git). Process environment variables override `.env`; `APP_ENV_FILE=/path/file` makes the loader read that one file instead of `.env` (it must exist; it is a pointer, not a setting). Bootstrap sets it for every child command and drops every setting variable (and older `RETAIL_ANALYTICS_*`/`ANALYTICS_CLI_*` names) from your shell, so child commands see exactly: the env file's non-empty values, then everything else (PATH, `COMPOSE_*`, `DOCKER_*`); empty values count as unset. Names and the migration from the older prefixed names: see [Environment variables](#environment-variables).

- `APP_MODE=fixture` explicitly opts into offline fixed responses with no credentials.
- `GEMINI_MODEL` is the default model name used by the credential check.
- The investigation agent uses `AGENT_GEMINI_MODEL` (default `gemini-3.8-flash`, Gemini Interactions API) as primary and `AGENT_OPENAI_MODEL` (default `gpt-5-mini`, OpenAI Responses API) as backup when `OPENAI_API_KEY` is set. First-token (60 s), streaming-stall (30 s) and per-request (180 s) limits, retries, fallback and per-attempt budget accounting are described in [docs/model-providers.md](model-providers.md).
- `APP_MODE=live` (default) requires the database URL, BigQuery project and Gemini API key (and the Temporal address with Temporal execution); all missing settings are reported together.
- `EXECUTION_BACKEND=local` (default) or `temporal`: see [Temporal execution (opt-in)](#temporal-execution-opt-in). `LOCAL_MAX_CONCURRENT_RUNS` (4) and `LOCAL_SHUTDOWN_GRACE_SECONDS` (10) bound local execution.
- `REFERENCE_KEY` (at least 32 bytes) is the master key for opaque customer, order and item references. It is optional: when it is unset, queries that need references fail closed. Never commit or log it.

Errors name the variable and the problem, never the value. All settings are declared in `src/retail_analytics/bootstrap/config.py`; add new ones there and to `.env.example` (a test keeps them in sync). Only bootstrap reads configuration; inner layers receive typed values.

### Environment variables

Settings use plain names: the backend reads each setting from its own variable (for example `GEMINI_API_KEY`, `EXECUTION_BACKEND`, `AUTH_SIGNING_KEY`), the CLI uses `CLI_*`, and Docker Compose keys keep `COMPOSE_*`. Names that would be too generic in a shell get an `APP_` prefix (`APP_MODE`, `APP_DATABASE_URL`, `APP_API_HOST`, `APP_API_PORT`, `APP_ENV_FILE`); so a `DATABASE_URL` exported for another project never redirects the app or its migrations (bootstrap and `dev.sh` also drop these bare forms from child commands). The process environment is read only for these names; any other key in the env file (except `COMPOSE_*`) is rejected by name as a probable typo, with the closest known name suggested. `GEMINI_API_KEY`, `OPENAI_API_KEY` and `TEMPORAL_ADDRESS` are the names the provider SDKs and Temporal tools also use: a value exported in your shell overrides `.env`, as for every setting. A shell `OPENAI_API_KEY` therefore enables the GPT fallback; the API (and the worker) print the enabled providers once at startup, names only, for example `model providers: gemini (primary), openai (fallback, OPENAI_API_KEY set)`.

The older prefixed names (`RETAIL_ANALYTICS_*`, `ANALYTICS_CLI_*`) are no longer read and are refused, naming each key and its new name. To migrate an existing environment file in place, run `./scripts/bootstrap.sh --env-only` (add `--env-file FILE` for another file): it renames each old key, keeping values, order and comments; if a file sets both the old and the new name, the new one is kept, the old line is commented out and the conflict is reported (names only, never values). It is idempotent and never touches `COMPOSE_*` keys; a full `./scripts/bootstrap.sh` does the same. Old names exported in your shell must be unset or renamed by hand (bootstrap warns about them). Full mapping:

| Before | Now |
| --- | --- |
| `RETAIL_ANALYTICS_MODE` | `APP_MODE` |
| `RETAIL_ANALYTICS_API_HOST` | `APP_API_HOST` |
| `RETAIL_ANALYTICS_API_PORT` | `APP_API_PORT` |
| `RETAIL_ANALYTICS_ARTIFACT_DIR` | `ARTIFACT_DIR` |
| `RETAIL_ANALYTICS_ARTIFACT_MAX_MARKDOWN_BYTES` | `ARTIFACT_MAX_MARKDOWN_BYTES` |
| `RETAIL_ANALYTICS_ARTIFACT_MAX_BINARY_BYTES` | `ARTIFACT_MAX_BINARY_BYTES` |
| `RETAIL_ANALYTICS_DATABASE_URL` | `APP_DATABASE_URL` |
| `RETAIL_ANALYTICS_EXECUTION_BACKEND` | `EXECUTION_BACKEND` |
| `RETAIL_ANALYTICS_LOCAL_MAX_CONCURRENT_RUNS` | `LOCAL_MAX_CONCURRENT_RUNS` |
| `RETAIL_ANALYTICS_LOCAL_SHUTDOWN_GRACE_SECONDS` | `LOCAL_SHUTDOWN_GRACE_SECONDS` |
| `RETAIL_ANALYTICS_TEMPORAL_ADDRESS` | `TEMPORAL_ADDRESS` |
| `RETAIL_ANALYTICS_TEMPORAL_NAMESPACE` | `TEMPORAL_NAMESPACE` |
| `RETAIL_ANALYTICS_TEMPORAL_TASK_QUEUE` | `TEMPORAL_TASK_QUEUE` |
| `RETAIL_ANALYTICS_BIGQUERY_PROJECT` | `GOOGLE_CLOUD_PROJECT` |
| `RETAIL_ANALYTICS_BIGQUERY_LOCATION` | `GOOGLE_CLOUD_LOCATION` |
| `RETAIL_ANALYTICS_SCHEMA_REFRESH_SECONDS` | `SCHEMA_REFRESH_SECONDS` |
| `RETAIL_ANALYTICS_EVIDENCE_CURRENT_FRESHNESS_SECONDS` | `EVIDENCE_CURRENT_FRESHNESS_SECONDS` |
| `RETAIL_ANALYTICS_AUDIT_RETENTION_DAYS` | `AUDIT_RETENTION_DAYS` |
| `RETAIL_ANALYTICS_CLEANUP_BATCH_SIZE` | `CLEANUP_BATCH_SIZE` |
| `RETAIL_ANALYTICS_GEMINI_API_KEY` | `GEMINI_API_KEY` |
| `RETAIL_ANALYTICS_GEMINI_MODEL` | `GEMINI_MODEL` |
| `RETAIL_ANALYTICS_OPENAI_API_KEY` | `OPENAI_API_KEY` |
| `RETAIL_ANALYTICS_AGENT_GEMINI_MODEL` | `AGENT_GEMINI_MODEL` |
| `RETAIL_ANALYTICS_AGENT_OPENAI_MODEL` | `AGENT_OPENAI_MODEL` |
| `RETAIL_ANALYTICS_MODEL_FIRST_TOKEN_SECONDS` | `MODEL_FIRST_TOKEN_SECONDS` |
| `RETAIL_ANALYTICS_MODEL_STREAM_STALL_SECONDS` | `MODEL_STREAM_STALL_SECONDS` |
| `RETAIL_ANALYTICS_MODEL_REQUEST_MAX_SECONDS` | `MODEL_REQUEST_MAX_SECONDS` |
| `RETAIL_ANALYTICS_MODEL_PRIMARY_COOLDOWN_SECONDS` | `MODEL_PRIMARY_COOLDOWN_SECONDS` |
| `RETAIL_ANALYTICS_MODEL_MAX_OUTPUT_TOKENS` | `MODEL_MAX_OUTPUT_TOKENS` |
| `RETAIL_ANALYTICS_EMBEDDING_PROVIDER` | `EMBEDDING_PROVIDER` |
| `RETAIL_ANALYTICS_EMBEDDING_MODEL` | `EMBEDDING_MODEL` |
| `RETAIL_ANALYTICS_EMBEDDING_DIMENSIONS` | `EMBEDDING_DIMENSIONS` |
| `RETAIL_ANALYTICS_EXCHANGE_RATE_BASE_URL` | `EXCHANGE_RATE_BASE_URL` |
| `RETAIL_ANALYTICS_SOURCE_CURRENCY_DECLARED` | `SOURCE_CURRENCY_DECLARED` |
| `RETAIL_ANALYTICS_RETRIEVAL_MAX_RESULTS` | `RETRIEVAL_MAX_RESULTS` |
| `RETAIL_ANALYTICS_RETRIEVAL_CHANNEL_CANDIDATES` | `RETRIEVAL_CHANNEL_CANDIDATES` |
| `RETAIL_ANALYTICS_RETRIEVAL_MIN_SIMILARITY` | `RETRIEVAL_MIN_SIMILARITY` |
| `RETAIL_ANALYTICS_RETRIEVAL_MIN_LEXICAL_COVERAGE` | `RETRIEVAL_MIN_LEXICAL_COVERAGE` |
| `RETAIL_ANALYTICS_RETRIEVAL_SEMANTIC_WEIGHT` | `RETRIEVAL_SEMANTIC_WEIGHT` |
| `RETAIL_ANALYTICS_RUN_ACTIVE_SECONDS` | `RUN_ACTIVE_SECONDS` |
| `RETAIL_ANALYTICS_RUN_MAX_PROVIDER_REQUESTS` | `RUN_MAX_PROVIDER_REQUESTS` |
| `RETAIL_ANALYTICS_RUN_MAX_TOKENS` | `RUN_MAX_TOKENS` |
| `RETAIL_ANALYTICS_RUN_MAX_QUERIES` | `RUN_MAX_QUERIES` |
| `RETAIL_ANALYTICS_QUERY_MAX_BYTES` | `QUERY_MAX_BYTES` |
| `RETAIL_ANALYTICS_RUN_MAX_BYTES` | `RUN_MAX_BYTES` |
| `RETAIL_ANALYTICS_QUERY_MAX_CORRECTIONS` | `QUERY_MAX_CORRECTIONS` |
| `RETAIL_ANALYTICS_MAX_TRANSIENT_ATTEMPTS` | `MAX_TRANSIENT_ATTEMPTS` |
| `RETAIL_ANALYTICS_RETRY_BASE_SECONDS` | `RETRY_BASE_SECONDS` |
| `RETAIL_ANALYTICS_RETRY_MAX_SECONDS` | `RETRY_MAX_SECONDS` |
| `RETAIL_ANALYTICS_QUERY_DEADLINE_SECONDS` | `QUERY_DEADLINE_SECONDS` |
| `RETAIL_ANALYTICS_RESULT_MAX_ROWS` | `RESULT_MAX_ROWS` |
| `RETAIL_ANALYTICS_RESULT_MAX_BYTES` | `RESULT_MAX_BYTES` |
| `RETAIL_ANALYTICS_AUTH_ISSUER` | `AUTH_ISSUER` |
| `RETAIL_ANALYTICS_AUTH_AUDIENCE` | `AUTH_AUDIENCE` |
| `RETAIL_ANALYTICS_AUTH_SIGNING_KEY` | `AUTH_SIGNING_KEY` |
| `RETAIL_ANALYTICS_TELEMETRY_ENABLED` | `TELEMETRY_ENABLED` |
| `RETAIL_ANALYTICS_TELEMETRY_TRACES_ENDPOINT` | `TELEMETRY_TRACES_ENDPOINT` |
| `RETAIL_ANALYTICS_TELEMETRY_METRICS_ENDPOINT` | `TELEMETRY_METRICS_ENDPOINT` |
| `RETAIL_ANALYTICS_TELEMETRY_EXPERIMENT_ID` | `TELEMETRY_EXPERIMENT_ID` |
| `RETAIL_ANALYTICS_TELEMETRY_EXPORT_TIMEOUT_SECONDS` | `TELEMETRY_EXPORT_TIMEOUT_SECONDS` |
| `RETAIL_ANALYTICS_TELEMETRY_METRIC_INTERVAL_SECONDS` | `TELEMETRY_METRIC_INTERVAL_SECONDS` |
| `RETAIL_ANALYTICS_TELEMETRY_CAPTURE_CONTENT` | `TELEMETRY_CAPTURE_CONTENT` |
| `RETAIL_ANALYTICS_REFERENCE_KEY` | `REFERENCE_KEY` |
| `RETAIL_ANALYTICS_ENV_FILE` | `APP_ENV_FILE` |
| `ANALYTICS_CLI_API_URL` | `CLI_API_URL` |
| `ANALYTICS_CLI_TIMEOUT_SECONDS` | `CLI_TIMEOUT_SECONDS` |
| `ANALYTICS_CLI_TOKEN` | `CLI_TOKEN` |
| `ANALYTICS_CLI_TOKEN_FILE` | `CLI_TOKEN_FILE` |

## How to add a bootstrap step

Everything bootstrap does is one ordered tuple, `STEPS` in `src/retail_analytics/bootstrap/local_setup.py`. A later feature adds its own step there instead of writing a separate script. The example below is illustrative (`seed_example` is not a real module):

```python
def step_example(ctx: SetupContext) -> StepResult:
    ctx.python("-m", "retail_analytics.bootstrap.seed_example", show=True)
    return StepResult("done", "example data seeded")


STEPS = (
    ...,
    BootstrapStep("example", "seed example data", step_example),
    ...,
)  # after what it needs
```

Rules: the step must be idempotent (a second run changes nothing); it runs the project's own command with the environment file's values (`ctx.python`, `ctx.compose`, `ctx.run`; output is scrubbed of secret values); it raises `StepFailed` with a message that has no secrets; set `required=False` for a check that should only warn, and `enabled=` for opt-in steps. Put the step after the steps it depends on (migrations before seeds). A new setting with a code default goes into `.env.example` as a `# NAME=default` comment (it is not written to `.env`); one that must be set or generated locally is an uncommented line (read dynamically, so existing `.env` files gain it on the next run). When you change a default, add the old value to `HISTORICAL_DEFAULTS` in `bootstrap/local_env.py` so bootstrap flags env files that still pin it; a secret that is safe to generate locally is added to `GENERATED_SECRETS` in `bootstrap/local_env.py`; a credential it cannot generate goes in `EXTERNAL_CREDENTIALS` with the action to take. Cover it in `tests/unit/test_local_bootstrap.py`, and add an assertion to the Docker test `tests/integration/test_local_bootstrap.py` if the step provisions state.

## Source data profile

`python -m retail_analytics.bootstrap.profile_source` (needs a BigQuery project and application default credentials) profiles the four public source tables with bounded aggregate queries only, checks the catalog mappings against live metadata, runs two compiled analyses through the real job adapter and privacy boundary, and writes `docs/source-profile/source-profile.{json,md}`. The report holds counts and ranges, never personal values; the source metadata names no currency, so the currency stays unknown.

## Evaluation runner

`retail-analytics-eval` (or `python -m retail_analytics.bootstrap.evaluate`) runs a versioned JSON scenario manifest against a pluggable target and writes a versioned result file.

```sh
retail-analytics-eval run --manifest MANIFEST.json --target replay --observations OBS.json \
  --out evaluation-results/run.json --version model=NAME --version dataset=SNAPSHOT [--baseline PREV.json]
retail-analytics-eval compare BASELINE.json CURRENT.json
retail-analytics-eval summary evaluation-results/run.json
```

- Targets: `replay` (recorded observations, deterministic, no agent needed) or `package.module:factory` returning an object with `target_id` and `run(ScenarioInput) -> TargetObservation` (the `EvaluationTarget` port in `application/ports/evaluation.py`; contracts in `application/contracts/evaluation.py`). Targets never see expectations.
- Statuses: `passed`, `failed`, `errored`, `skipped` (not implemented or other mode), `blocked` (missing capability such as `--capability bigquery`, unavailable target or judge) and `scored` (judge/operational only). Exit codes: 0 passed, 1 failed, 3 incomplete; blocked and skipped cases never count as passing.
- Results (`schema_version` 1, `application/evaluation/results.py`) keep deterministic checks, judge scores and operational measurements in separate sections, state every denominator (null ratio when zero), record manifest digest and model/config/prompt/persona/metric/policy/dataset/retrieval/corpus versions, and store no raw text: strings appear as digests and the writer refuses output that looks like PII or a credential. Identical inputs give byte-identical files and the same `verdict_digest`; `recorded_at` appears only with `--timestamp`.
- Result files are local artifacts (`evaluation-results/` is ignored).
- Judges: scenarios whose `verification` includes `judge` carry a `JudgeSpec` (rubric id and dimensions; currently `report-quality-v1`). `run_manifest(..., judges=[...])` accepts `JudgeScorer` implementations and records their `judge_ids`; the command wires none yet, so those scenarios are `blocked (judge_unavailable)`. The rubric anchors, the proposed judges and how to wire a scorer are in [evaluation/judges](../evaluation/judges/README.md).

### Agent runtime target

`retail_analytics.bootstrap.agent_evaluation` provides the `agent_runtime` target: every scenario runs as real investigations (guarded model steps, permission-filtered tools, compiler, result privacy boundary, evidence, reports and the output gate) against an offline DuckDB warehouse instead of BigQuery. It uses the configured execution backend: with local execution (default) it needs only the local PostgreSQL (migrated); with `EXECUTION_BACKEND=temporal`, or the explicit `heldout_scripted_temporal` / `realdata_scripted_temporal` factories, it runs Temporal workflows on an in-process worker and also needs Temporal. The result records the backend in its target ID (`agent_runtime:local` or `agent_runtime:temporal`). If the services are unreachable, or another local-execution process holds the database, every case is `blocked`. Each scenario gets its own evaluation executive (stable per scenario, so opaque references are reproducible) and a new session.

```sh
retail-analytics-eval run --manifest evaluation/heldout/manifest.json \
  --target retail_analytics.bootstrap.agent_evaluation:heldout_scripted \
  --capability agent_runtime --out evaluation-results/heldout-scripted.json
retail-analytics-eval run --manifest evaluation/realdata/manifest.json \
  --target retail_analytics.bootstrap.agent_evaluation:realdata_scripted \
  --capability agent_runtime --capability frozen_extract_source \
  --out evaluation-results/realdata-scripted.json
```

- `heldout_*` answers from the synthetic held-out fixture; `realdata_*` from the frozen real-data extract (`frozen_extract_source`). Live BigQuery is never scored against frozen values.
- Scripted results are expected to match across backends (same application steps and agent); recovery after a process restart differs and is covered by the Docker suites, not by these manifests.
- `*_scripted` plays reviewed plans (`evaluation/agent-scripts/*.json`, some deliberately adversarial) instead of a model. It measures the runtime and its guards under a known plan, not model planning quality; report it as scripted. `*_live` uses the configured provider chain and counts against provider quotas.
- Values are read from released evidence (single-row results by column name). Safety flags come from evidence, released text and tool calls against fixture canaries; report-element flags (definition disclosed, contributors, causal wording, partial periods) are textual heuristics until a judge scores those dimensions.
- The DuckDB oracle rewrites `GROUP BY <output name>` to positions because DuckDB, unlike BigQuery, does not resolve an ambiguous name to the SELECT alias.

## Golden retrieval benchmark

`python -m retail_analytics.bootstrap.retrieval_eval` measures precision@k, recall@k, MRR, nDCG, no-match behavior and access violations for keyword-only, semantic-only and fused retrieval on labeled questions with separate tuning and held-out splits. Labels, corpus, method, measured results and limits are in [evaluation/retrieval](../evaluation/retrieval/README.md). It uses the runner's manifest and result format.

## Package layout and dependency rules

```text
src/retail_analytics/
  domain/        business records, value objects, pure policies (standard library only)
  application/   use cases, permission/budget gates, the narrow ports they own
  capabilities/  typed analytical/report/knowledge capability handlers
  adapters/      PostgreSQL, BigQuery, SQLGlot, models, Pydantic AI/Temporal, artifacts, telemetry
  interfaces/    HTTP/SSE (FastAPI) and CLI (Click) translation
  bootstrap/     configuration and composition roots for API, worker and CLI
```

Dependencies point inward:

| Layer | May import project layers | May import third-party packages |
| --- | --- | --- |
| domain | domain | none |
| application | domain, application | pydantic |
| capabilities | domain, application, capabilities | pydantic |
| adapters | domain, application, capabilities, adapters | any |
| interfaces | domain, application, capabilities, interfaces | pydantic, FastAPI/Starlette, Click, httpx |
| bootstrap | all | any |

Domain, application and capabilities also may not use `importlib`, `subprocess` or `socket`. The rules live in one place, `tests/architecture/boundaries.py`, and are enforced two ways:

- a static check parses every module, including imports inside functions and `TYPE_CHECKING` blocks;
- an import-time check imports each inner layer in a fresh interpreter and fails if a forbidden SDK is loaded (even transitively) or if importing opens files, reads environment variables, opens sockets or starts processes.

A deliberately invalid package in `tests/architecture/fixtures/` proves both checks fail on reverse dependencies and forbidden SDK imports. A justified exception goes in `EXCEPTIONS` in `boundaries.py` with its reason; do not weaken the rules to make a dependency pass.

The same module enforces the application layout (`check_application_layout`, statically, with its own invalid fixture in `tests/architecture/fixtures/invalid_layout/`):

- `typing.Protocol` classes under `application/` live only in `application/ports/`, and ports contain only Protocols (bodiless methods), imports and type aliases.
- `application/contracts/` holds data types only: no Protocols, no service-like classes (service-style names, async methods, injected-collaborator `__init__`; error types may carry data), and `contracts/__init__.py` keeps `ContractModel`, `Identifier` and `CONTRACT_VERSION`.
- Ports and contracts never import service modules (`application.<area>`), adapters, interfaces, bootstrap or capabilities, and contracts never import ports. Adapters take port types from `application.ports`, not from a service module.
- Layout exceptions use the same `EXCEPTIONS` table, keyed by (module, class or imported module), and need a reason.

## Dependencies

Direct dependencies are pinned exactly in `pyproject.toml`, one per line, sorted by name (`dependencies` for runtime, the `dev` extra for tooling). `requirements.txt` is the fully pinned, cross-platform lock generated from them:

```sh
./scripts/lock.sh    # uv pip compile requirements.in --universal --python-version 3.12 --no-annotate
```

Rerun it after changing any pin and commit both files. On a merge conflict in `requirements.txt`, resolve `pyproject.toml` first and regenerate rather than hand-merging.


Older checkouts using `BIGQUERY_PROJECT` and `BIGQUERY_LOCATION` can run
`./scripts/bootstrap.sh --env-only` to rename them to `GOOGLE_CLOUD_PROJECT`
and `GOOGLE_CLOUD_LOCATION` without changing their values. Update exported
shell variables to the new names too.
