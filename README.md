# Retail Analytics Agent

A conversational analytics assistant for retail executives. It investigates business questions using BigQuery data and curated analyst knowledge, supports follow-up exploration, and produces reports with evidence and action items.

## Project status

Early implementation. The application skeleton, configuration validation and repository checks exist; analytical behavior does not yet. The intended application uses a CLI connected to an HTTP backend, Pydantic AI for the agent, PostgreSQL for application state, and either in-process execution inside the API (the local default) or Temporal for durable execution (opt-in). BigQuery provides read-only retail analysis; model and database credentials stay on the backend.

Setup for live services, public architecture documentation and evaluation results will be added as their implementations are verified.

Do not commit credentials, raw query results or private conversation data.

## Architecture

```mermaid
flowchart LR
    cli["analytics CLI"] -- "HTTPS + SSE" --> api["API<br/>auth, sessions, confirmations"]
    api --> runner["Investigation runner<br/>local (default) or Temporal (opt-in)"]
    runner --> agent["Pydantic AI agent"]
    agent -- "tool calls" --> guards["Guards: SQL compiler,<br/>privacy gates, budgets"]
    agent --> models["Gemini, GPT backup"]
    guards --> bq[("BigQuery")]
    api --> pg[("PostgreSQL + artifacts")]
    guards --> pg
```

The high-level design, production reference deployment, data flow, technology
choices and requirement-by-requirement coverage are in
[docs/architecture](docs/architecture/README.md).

## Quick start

On a new machine with Docker (Compose v2) and Python 3.12 (or [uv](https://docs.astral.sh/uv/)) installed:

```sh
./scripts/bootstrap.sh
```

That one command is idempotent and does everything needed for a working, seeded local environment in fixture mode (offline, no credentials):

1. creates `.venv` and installs the pinned dependencies if no virtualenv is active;
2. creates `.env` from `.env.example`, or only adds the keys an existing `.env` lacks. It never overwrites or reorders a value, generates local-only secrets (`RETAIL_ANALYTICS_AUTH_SIGNING_KEY`, `RETAIL_ANALYTICS_REFERENCE_KEY`, and the database passwords for a new Compose volume) with `secrets`, and fills the connection defaults. It prints `<generated>`, `<kept>`, `<default>` or `<missing: action>` per key, never a value;
3. checks Docker, starts PostgreSQL and waits until it is healthy (Temporal only when [selected](#temporal-execution-opt-in));
4. runs `alembic upgrade head`;
5. provisions the demo executives and seeds the Golden knowledge library;
6. validates the configuration and, when BigQuery and Gemini are configured, checks that access.

Then start the backend with one command and call it with a dev token; see [HTTP and SSE API](docs/http-api.md) and the [CLI guide](docs/cli.md):

```sh
./scripts/dev.sh        # or: python -m retail_analytics.bootstrap.dev_up
```

By default investigations run inside the API process (local execution): `dev.sh` needs only PostgreSQL, and no Temporal server or worker is started. A run keeps going when the CLI disconnects; stopping the API ends running investigations as interrupted (they are not resumed; send the request again). Durable Temporal execution is [opt-in](#temporal-execution-opt-in).

`dev.sh` makes sure PostgreSQL, the telemetry stack (MLflow, Prometheus, Grafana; skip with `--no-telemetry`) and the migrations are in place (the same bootstrap steps; unrelated containers are never touched), starts `retail-analytics-api` with the same environment file and `[api]`-prefixed logs, waits until `/healthz` answers for the selected execution backend, then prints the API URL, the Grafana and MLflow URLs and the command that issues a dev token (the token and secrets are never printed). Ctrl-C or SIGTERM stops what it started (SIGTERM, then SIGKILL after 10 s; with local execution after `RETAIL_ANALYTICS_LOCAL_SHUTDOWN_GRACE_SECONDS` + 5 s, so the API first ends its running investigations as interrupted) and exits 0; if a process exits on its own, the command exits 1 naming it. It refuses to start if the API port is taken by something else. Options: `--env-file FILE` (the same isolation as bootstrap: only that file is read, parent-shell `RETAIL_ANALYTICS_*` variables are dropped), `--project NAME` (Compose project), `--execution-backend local|temporal` (this run only), `--no-services` (do not touch Docker; only check that the needed services are reachable), `--no-telemetry` (do not start the telemetry stack; `--telemetry` is accepted and does nothing), `--ready-timeout SECONDS`. It is a local-development convenience, not a supervisor.

External credentials (BigQuery project, Gemini key, optional OpenAI key) cannot be generated: they stay empty with a pointer to [docs/google-access.md](docs/google-access.md), and fixture mode works without them. Add them to `.env` and rerun, or use `--interactive` to be asked (secrets use hidden input). Never regenerate a non-empty `RETAIL_ANALYTICS_REFERENCE_KEY`: rotating it invalidates every existing customer reference.

Options: MLflow, Prometheus and Grafana start by default and the next steps print their URLs; `--no-telemetry` skips them (and writes `RETAIL_ANALYTICS_TELEMETRY_ENABLED=false` when that key is new), `--telemetry` is accepted and does nothing; an existing env file without the key gets `true`, and an explicit `false` is never overwritten (the stack is then not started either); `--env-file FILE` works on another environment file (the Compose and every child command then use only its values; the repository's `.env` is never read or changed); `--project NAME`, `--postgres-port`, `--temporal-port` pick an isolated Compose project and free ports; `--execution-backend local|temporal` selects the backend for this run (written to the env file only when the key is new); `--env-only` only creates or completes the env file; `--list-steps` prints the ordered steps.

### Temporal execution (opt-in)

`RETAIL_ANALYTICS_EXECUTION_BACKEND` selects where investigations execute, independently of fixture/live mode: `local` (default) runs them in the API process over PostgreSQL; `temporal` runs them as durable Temporal workflows on `retail-analytics-worker`, which resume after a process restart. To use Temporal, set `RETAIL_ANALYTICS_EXECUTION_BACKEND=temporal` in `.env` (or pass `--execution-backend temporal` to `bootstrap.sh`/`dev.sh` for one run). Bootstrap and `dev.sh` then also start the Temporal server and its namespace, and `dev.sh` runs the worker next to the API with `[worker]`/`[api]` logs and waits for the worker's Temporal connection; `RETAIL_ANALYTICS_TEMPORAL_ADDRESS` is required (bootstrap fills the local default). With local execution `retail-analytics-worker` exits at once with an instruction.

Existing environment files: one without the setting runs local, even if it still has a Temporal address; the next `./scripts/bootstrap.sh` adds `RETAIL_ANALYTICS_EXECUTION_BACKEND=local` and explains it, keeping Temporal values, containers and volumes. An explicit `temporal` is never changed. Runs are never moved between backends: if the other backend still has active investigations or queued requests, the API refuses to start, lists them and says how to finish or cancel them with their original backend. Details, guarantees and limits: [investigation runtime](docs/investigation-runtime.md). The production design keeps Temporal with separately scaled workers; local execution is the single-process local topology.

### How to add a bootstrap step

Everything bootstrap does is one ordered tuple, `STEPS` in `src/retail_analytics/bootstrap/local_setup.py`. A later feature (an API, persona seeds, an embeddings backfill) adds its own step there instead of writing a separate script:

```python
def step_personas(ctx: SetupContext) -> StepResult:
    ctx.python("-m", "retail_analytics.bootstrap.seed_personas", show=True)
    return StepResult("done", "personas seeded")


STEPS = (
    ...,
    BootstrapStep("personas", "seed personas", step_personas),
    ...,
)  # after what it needs
```

Rules: the step must be idempotent (a second run changes nothing); it runs the project's own command with the environment file's values (`ctx.python`, `ctx.compose`, `ctx.run`; output is scrubbed of secret values); it raises `StepFailed` with a message that has no secrets; set `required=False` for a check that should only warn, and `enabled=` for opt-in steps. Put the step after the steps it depends on (migrations before seeds). A new setting goes into `.env.example` with a safe local default (read dynamically, so existing `.env` files gain it on the next run); a secret that is safe to generate locally is added to `GENERATED_SECRETS` in `bootstrap/local_env.py`; a credential it cannot generate goes in `EXTERNAL_CREDENTIALS` with the action to take. Cover it in `tests/unit/test_local_bootstrap.py`, and add an assertion to the Docker test `tests/integration/test_local_bootstrap.py` if the step provisions state.

## Development

Requires Python 3.12 (`requires-python = ">=3.12,<3.13"`, also in `.python-version`). [uv](https://docs.astral.sh/uv/) is recommended for creating the environment and regenerating the lock file; plain `pip` installs work too. Run commands from the repository root.

```sh
uv venv --python 3.12              # or: python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # package (editable) + pinned deps + dev tools
./scripts/check.sh                 # every check; must pass before each commit
```

### Checks

`./scripts/check.sh` runs, in order:

| Check | Command |
| --- | --- |
| Lint | `ruff check .` |
| Formatting | `ruff format --check .` (fix with `ruff format .`) |
| Types (strict) | `mypy` |
| Tests, including architecture | `python -m pytest` |

Architecture checks alone: `python -m pytest tests/architecture`. Tests run offline in fixture mode, and `tests/conftest.py` hides any local `RETAIL_ANALYTICS_*`/`ANALYTICS_CLI_*` variables and `.env` from them. Tests that need real BigQuery or model credentials use the `live` marker and skip when credentials are absent; a skipped test is never reported as passing.

### Entry points

| Command | Module | Purpose |
| --- | --- | --- |
| `analytics` | `retail_analytics.bootstrap.cli` | CLI client and prototype UI; talks to the backend over HTTP only (`analytics chat`). See [CLI guide](docs/cli.md) |
| `retail-analytics-api` | `retail_analytics.bootstrap.api` | Authenticated HTTP/SSE investigation API; needs PostgreSQL and the signing key, and runs the investigations itself with local execution (Temporal execution: also Temporal and the worker). See [HTTP and SSE API](docs/http-api.md) |
| `retail-analytics-check-credentials` | `retail_analytics.bootstrap.check_credentials` | Verify BigQuery and Gemini access without printing secrets; see [Google access setup](docs/google-access.md) |
| `retail-analytics-worker` | `retail_analytics.bootstrap.worker` | Temporal investigation worker, only with `RETAIL_ANALYTICS_EXECUTION_BACKEND=temporal` (fixture model, or the live Gemini/GPT chain); exits with status 3 otherwise |
| `retail-analytics-dev-access` | `retail_analytics.bootstrap.dev_access` | Development only: provision the two synthetic executives and issue local tokens; see [Authentication and entitlements](#authentication-and-entitlements) |

Live mode (`RETAIL_ANALYTICS_MODE=live`) also requires `RETAIL_ANALYTICS_AUTH_SIGNING_KEY`, and the API requires it in every mode (no route skips authentication).

Backend entry points accept `--check-config`: validate settings, print them with secrets shown only as `<set>`/`<unset>`, and exit. Invalid configuration exits with status 2.

### Configuration

`./scripts/bootstrap.sh` creates `.env` for you (see Quick start); to do it by hand, copy `.env.example` to `.env` (ignored by Git). Process environment variables override `.env`; `RETAIL_ANALYTICS_ENV_FILE=/path/file` makes the loader read that one file instead of `.env` (it must exist; it is a pointer, not a setting). Bootstrap sets it for every child command and drops stray `RETAIL_ANALYTICS_*`/`ANALYTICS_CLI_*` variables from your shell, so child commands see exactly: the env file's non-empty values, then everything else unprefixed (PATH, `COMPOSE_*`, `DOCKER_*`); empty values count as unset. Backend settings use the `RETAIL_ANALYTICS_` prefix and the CLI uses `ANALYTICS_CLI_`. Unknown prefixed variables are rejected by name to catch typos.

- `RETAIL_ANALYTICS_MODE=fixture` (default) runs offline with no credentials.
- `RETAIL_ANALYTICS_GEMINI_MODEL` is the default model name used by the credential check.
- The investigation agent uses `RETAIL_ANALYTICS_AGENT_GEMINI_MODEL` (default `gemini-3.8-flash`, Gemini Interactions API) as primary and `RETAIL_ANALYTICS_AGENT_OPENAI_MODEL` (default `gpt-5-mini`, OpenAI Responses API) as backup when `RETAIL_ANALYTICS_OPENAI_API_KEY` is set. First-token (60 s), streaming-stall (30 s) and per-request (180 s) limits, retries, fallback and per-attempt budget accounting are described in [docs/model-providers.md](docs/model-providers.md).
- `RETAIL_ANALYTICS_MODE=live` requires the database URL, BigQuery project and Gemini API key (and the Temporal address with Temporal execution); all missing settings are reported together.
- `RETAIL_ANALYTICS_EXECUTION_BACKEND=local` (default) or `temporal`: see [Temporal execution (opt-in)](#temporal-execution-opt-in). `RETAIL_ANALYTICS_LOCAL_MAX_CONCURRENT_RUNS` (4) and `RETAIL_ANALYTICS_LOCAL_SHUTDOWN_GRACE_SECONDS` (10) bound local execution.
- `RETAIL_ANALYTICS_REFERENCE_KEY` (at least 32 bytes) is the master key for opaque customer, order and item references. It is optional: when it is unset, queries that need references fail closed. Never commit or log it.

Errors name the variable and the problem, never the value. All settings are declared in `src/retail_analytics/bootstrap/config.py`; add new ones there and to `.env.example` (a test keeps them in sync). Only bootstrap reads configuration; inner layers receive typed values.

### Source data profile

`python -m retail_analytics.bootstrap.profile_source` (needs a BigQuery project and application default credentials) profiles the four public source tables with bounded aggregate queries only, checks the catalog mappings against live metadata, runs two compiled analyses through the real job adapter and privacy boundary, and writes `docs/source-profile/source-profile.{json,md}`. The report holds counts and ranges, never personal values; the source metadata names no currency, so the currency stays unknown.

### Evaluation runner

`retail-analytics-eval` (or `python -m retail_analytics.bootstrap.evaluate`) runs a versioned JSON scenario manifest against a pluggable target and writes a versioned result file.

```sh
retail-analytics-eval run --manifest MANIFEST.json --target replay --observations OBS.json \
  --out evaluation-results/run.json --version model=NAME --version dataset=SNAPSHOT [--baseline PREV.json]
retail-analytics-eval compare BASELINE.json CURRENT.json
retail-analytics-eval summary evaluation-results/run.json
```

- Targets: `replay` (recorded observations, deterministic, no agent needed) or `package.module:factory` returning an object with `target_id` and `run(ScenarioInput) -> TargetObservation` (`application/evaluation/ports.py`). Targets never see expectations.
- Statuses: `passed`, `failed`, `errored`, `skipped` (not implemented or other mode), `blocked` (missing capability such as `--capability bigquery`, unavailable target or judge) and `scored` (judge/operational only). Exit codes: 0 passed, 1 failed, 3 incomplete; blocked and skipped cases never count as passing.
- Results (`schema_version` 1, `application/evaluation/results.py`) keep deterministic checks, judge scores and operational measurements in separate sections, state every denominator (null ratio when zero), record manifest digest and model/config/prompt/persona/metric/policy/dataset/retrieval/corpus versions, and store no raw text: strings appear as digests and the writer refuses output that looks like PII or a credential. Identical inputs give byte-identical files and the same `verdict_digest`; `recorded_at` appears only with `--timestamp`.
- Result files are local artifacts (`evaluation-results/` is ignored).

#### Agent runtime target

`retail_analytics.bootstrap.agent_evaluation` provides the `agent_runtime` target: every scenario runs as real investigations (guarded model steps, permission-filtered tools, compiler, result privacy boundary, evidence, reports and the output gate) against an offline DuckDB warehouse instead of BigQuery. It uses the configured execution backend: with local execution (default) it needs only the local PostgreSQL (migrated); with `RETAIL_ANALYTICS_EXECUTION_BACKEND=temporal`, or the explicit `heldout_scripted_temporal` / `realdata_scripted_temporal` factories, it runs Temporal workflows on an in-process worker and also needs Temporal. The result records the backend in its target ID (`agent_runtime:local` or `agent_runtime:temporal`). If the services are unreachable, or another local-execution process holds the database, every case is `blocked`. Each scenario gets its own evaluation executive (stable per scenario, so opaque references are reproducible) and a new session.

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

### Golden retrieval benchmark

`python -m retail_analytics.bootstrap.retrieval_eval` measures precision@k, recall@k, MRR, nDCG, no-match behavior and access violations for keyword-only, semantic-only and fused retrieval on labeled questions with separate tuning and held-out splits. Labels, corpus, method, measured results and limits are in `evaluation/retrieval/README.md`. It uses the runner's manifest and result format.

### Local services (PostgreSQL, and Temporal when selected)

`compose.yaml` runs one PostgreSQL 17 server and Temporal 1.32 (image digests pinned), both bound to loopback only. Requires Docker with Compose v2. Passwords are throwaway local defaults; override with `COMPOSE_APP_DB_PASSWORD`, `COMPOSE_TEMPORAL_DB_PASSWORD`, `COMPOSE_PG_ADMIN_PASSWORD`.

```sh
docker compose up -d --wait postgres               # project retail-analytics-local (local execution)
docker compose up -d --wait temporal               # Temporal execution only
docker compose run --rm temporal-namespace         #   and its namespace (7-day closed-history retention)
export RETAIL_ANALYTICS_DATABASE_URL=postgresql+psycopg://retail_app:local-only-app@127.0.0.1:55442/retail_app
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

Migrations live in `migrations/` (Alembic, configured by `alembic.ini`; the URL comes from `RETAIL_ANALYTICS_DATABASE_URL` or `.env`). The baseline revision creates `app_meta`; revision `0002` adds sessions, messages, runs, tool executions (with BigQuery job detail) and the append-only execution and run event histories; revision `0003` adds executives and product entitlements. Add new revisions with `alembic revision -m "..."` chained after the current head; a test keeps the history a single linear chain.

Application state is reached through the narrow ports in `retail_analytics.application.ports.persistence`, implemented with SQLAlchemy Core in `retail_analytics.adapters.postgres` and wired by `retail_analytics.bootstrap.persistence`. Retried writes are idempotent on application-generated keys (the operation ID for tool executions, the submission key for runs); reusing a key for different content raises a typed conflict. A session has at most one active run, enforced by a row lock and a partial unique index. Run events carry a gap-free per-run sequence for replay after a client's last received event ID.

Docker-dependent tests carry the `docker` marker and are excluded from `./scripts/check.sh`. Run them with `python -m pytest -m docker`; they start their own uniquely named Compose project on free ports and remove it afterwards.

### Authentication and entitlements

Assistant users are executives, distinct from the customers in the dataset. Three things are kept apart:

- **Identity**: a validated bearer token (JWT) names the caller by issuer and subject, mapped to one active row in `executives`.
- **Operation permissions**: server-assigned roles grant permissions (`executive`: `analysis:read`, `reports:read_own`, `reports:delete_own`; `editor`: `persona:edit`; `reviewer`: `knowledge:review`; `admin`: `access:admin`). The token's `scope` claim can only narrow them: effective permissions are the intersection, so a token cannot add a permission the server did not grant. `admin` manages access and grants no product data.
- **Product entitlements**: the complete set of product IDs whose data an executive may see, stored in `product_entitlements`. No rows means no product data, never unrestricted access.

Every change to an executive's roles, products or active status increments `authorization_version` in the same transaction. Tools never receive identity or entitlements as arguments: `AccessResolver.context_for_run(principal, run_id)` (`retail_analytics.application.authorization`) checks that the run and its session belong to the caller, reloads current authority, and returns the `ExecutionContext` whose `ProductScope` carries the products and version. Call it again for every attempt, including inside retried activities, so an entitlement change applies to the next tool check. `OwnershipGuard` loads sessions, runs and operations only for their owner, and `require_owner` applies the same rule to other owned records. A record owned by someone else is indistinguishable from a missing one.

Each effective change also appends one `access.*` event to `audit_events` in that same transaction (`executive_registered`, `roles_changed`, `profile_changed`, `entitlements_changed`, `activated`, `deactivated`); if the event cannot be written the change rolls back, and identical repeats record nothing. Details hold the actor, change kind, role names, product counts and digests, and the old and new `authorization_version`, never product lists, labels or subjects. `AccessAdministration` methods take `actor_id` (default `system:operator`; the dev provisioning command and bootstrap record `system:dev-access`). `AccessAuditService.history(principal, executive_id)` (`bootstrap.access.build_access_audit`) lists an executive's changes newest first and requires `access:admin`.

**Local (simulated) authentication.** Tokens are HS256 JWTs signed with `RETAIL_ANALYTICS_AUTH_SIGNING_KEY` (at least 32 bytes; held only by the backend and the developer) for `RETAIL_ANALYTICS_AUTH_ISSUER` and `RETAIL_ANALYTICS_AUTH_AUDIENCE`. Verification accepts only HS256 and requires `iss`, `aud`, `sub`, `iat` and `exp`. It allows 30 seconds of clock skew and a lifetime of at most 24 hours. Forged, tampered, unsigned, expired, future-dated, wrong-issuer and wrong-audience tokens all fail with one uniform error that never includes the token. There is no setting or route that skips authentication, in either mode.

```sh
export RETAIL_ANALYTICS_AUTH_SIGNING_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
retail-analytics-dev-access provision          # needs RETAIL_ANALYTICS_DATABASE_URL and migrations at head
retail-analytics-dev-access token demo-a --minutes 60   # prints a token to stdout only
```

`provision` is idempotent. It creates `exec-demo-a` (roles executive and editor; product IDs 1–15989, the dataset's "Women" department) and `exec-demo-b` (roles executive and reviewer; product IDs 15990–29120, "Men"), so their data never overlaps. The split was checked against `thelook_ecommerce.products` on 2026-10-08. `token` issues a token whose scopes are the executive's current permissions. These identities are synthetic and for development only.

**Production identity (design only, not deployed).** A company identity provider issues the tokens (asymmetric signatures published as JWKS). A verifier for those keys replaces `LocalJwtAuthority` behind the same `TokenVerifier` port (`retail_analytics.application.authentication`). Executives are provisioned from the directory into `executives` by issuer and subject, and entitlements are still assigned server-side. The authorization path after verification does not change. No identity provider has been chosen or configured.

### Artifact storage

Report bodies (Markdown now; PNG/JPEG/PDF reserved) are stored as immutable files, not in PostgreSQL. `ArtifactService` (`application/artifacts.py`) is the interface other features use: `save`, `read`, `describe`, `versions`. Files live under `RETAIL_ANALYTICS_ARTIFACT_DIR` (default `data/local/artifacts`, gitignored; mount a Docker volume there) as `blobs/<artifact_id>/<sha256>`; table `artifact_versions` holds owner, version, media type, checksum, size and key. Limits: `RETAIL_ANALYTICS_ARTIFACT_MAX_MARKDOWN_BYTES` (1 MiB) and `..._MAX_BINARY_BYTES` (10 MiB); failures are `ArtifactError` with a code (`too_large`, `unsupported_media_type`, `invalid_content`, ...).

A save writes and fsyncs a temporary file, links it into place without overwriting, then commits the metadata row, so a crash never publishes a partial reference. Saves take an idempotency key (for example the operation ID); a retry returns the original version. Reads check ownership (not-found and not-owned are the same `AccessDenied`) and verify the checksum. `ArtifactMaintenance.reconcile()` removes old temporary files and unreferenced blobs (failed metadata commits) and reports metadata whose file is missing; `purge()` deletes an artifact's metadata, then its files.

### Golden seed library

Ten project-authored, reviewed example trios (question, SQL, report) live in `retail_analytics.application.golden_seed_library`. After the demo executives exist, `python -m retail_analytics.bootstrap.seed_knowledge` loads them through the normal submit and review lifecycle (author `demo-a`, reviewer `demo-b`) and is safe to rerun. Retrieval embeddings are stored in PostgreSQL (`golden_embeddings`, keyed by content digest, model and dimensions; vectors only, deleted when an example is erased); `python -m retail_analytics.bootstrap.warm_embeddings` fills them (a bootstrap step) so restarts make no provider calls. See [docs/golden-seeds.md](docs/golden-seeds.md) for the corpus, validation and the manual review checklist.

### Schema discovery and metadata caching

The model sees a reviewed logical catalog (`domain/logical_catalog.py`, versioned), never raw warehouse metadata. Each logical field is an allowlisted mapping to named source columns; a source column that no reviewed field maps (for example a newly added email-like column) stays unpublished. Direct identifiers cannot back any field, raw keys only become opaque references and exact age only an age band; the catalog rejects such definitions at construction.

`list_relations` and `describe_relation` (`capabilities/discovery.py`) render the same `CatalogView` that SQL validation consumes (`DiscoveryService.view_for(context)` in `application/discovery.py`), so shown fields and types are the usable ones.

- **Source schema cache.** One shared cache of column names and types (metadata only, no rows). It refreshes after `RETAIL_ANALYTICS_SCHEMA_REFRESH_SECONDS` (default 3600: the public dataset's schema changes rarely, and metadata reads are free) and on demand through `invalidate()` after a schema-mismatch error.
- **Per-executive filtering is never cached.** Each call builds the view from the freshly resolved `ExecutionContext`; no permission or an empty product scope gives an empty view, so entitlement changes apply immediately despite a warm cache. Results carry the catalog and entitlement versions.
- **Drift.** A missing column or table, or an incompatible type, disables the affected logical fields (the whole relation if the field is essential, such as a key) and any joins that need them. Other fields keep working. Errors name logical relations only, never warehouse tables or columns.
- **Outage.** If a refresh fails, the last validated snapshot is served (flagged stale) for up to 24 hours, retrying at most every 30 seconds; after that discovery fails closed with a temporary-failure error.

`tests/live/test_schema_metadata.py` checks the catalog against live table metadata when a project is configured.

### Golden retrieval

`bootstrap.retrieval.build_retrieval` returns a `GoldenRetriever`: eligibility prefilter on index entries (product scope, schema/metric applicability) before any scoring, then BM25 and vector channels fused by weighted reciprocal rank (k=60, semantic weight 2, keyword weight 1), at most 3 examples, none when no channel clears its threshold. Delivery goes only through `GoldenKnowledgeReader.deliver`, so stale index entries (retired, suspended, erased, changed) are refused and the next-ranked candidate is used. The index is in-process and rebuilt from `KnowledgeIndexSource` when the invalidation feed moves; vectors are cached by content digest. pgvector is not used: the pinned `postgres:17.11-alpine` image does not ship it and the corpus is tens of examples, so exact brute-force cosine is enough. Embeddings sit behind the `TextEmbedder` port: `hashing` (offline, deterministic, lexical only; the fixture default) or `gemini` (`gemini-embedding-2`, free-tier retries with backoff). Settings: `RETAIL_ANALYTICS_EMBEDDING_*` and `RETAIL_ANALYTICS_RETRIEVAL_*`. Defaults were measured in T36/T36-F1 for `gemini-embedding-2` (min similarity 0.70, min lexical coverage 0.75, semantic weight 2; see `evaluation/retrieval/README.md`); with the offline `hashing` embedder, unset thresholds keep the unmeasured 0.55 / 0.5 because cosine scales differ by model. Override with `RETAIL_ANALYTICS_RETRIEVAL_MIN_SIMILARITY`, `..._MIN_LEXICAL_COVERAGE` and `..._SEMANTIC_WEIGHT`.

### Restricted SQL compiler

The model never submits SQL for execution. It writes analytical SQL over the logical relations, and `SqlglotQueryCompiler` (`adapters/sql_compiler/`, behind the `QueryCompiler` port in `application/query_compiler.py`) compiles it against the executive's current `CatalogView` and `ProductScope`. Any query the compiler cannot fully understand is rejected.

- **Grammar.** Exactly one `SELECT`. Every SQLGlot node type and populated argument must be on an explicit allowlist (`grammar.py`). Writes, scripts, exports, set operations, window functions, `UNNEST`, system variables, unknown or user-defined functions and wildcard projections are rejected. `COUNT(*)` is allowed. Comments are stripped.
- **Resolution.** Sources are resolved by lexical scope, so a CTE or subquery named `customers` shadows the relation rather than binding to it. Every leaf must be a relation in the view, and every column must resolve to a published field or a derived output. Only an `ORDER BY` output alias may remain unqualified, which closes the unresolved-`HAVING` gap that SQLGlot qualification alone leaves open. The grammar is checked again after qualification (for example, a bare table alias that becomes a whole-row reference is rejected). Correlated subqueries, unused CTEs and duplicate output names are rejected.
- **Joins.** Only joins the catalog declares: a single equality from a relation already in the query to a new relation, `INNER` or `LEFT`, each relation at most once per query level. Joins through derived queries are rejected.
- **Binding.** Each logical relation is replaced by a trusted projection of only the referenced fields. The product scope (`@_policy_product_ids`) is applied at every physical read, before the model's query aggregates anything. Orders count only permitted items, and customers are reached only through permitted items. An empty scope is rejected outright. Opaque references and age bands come from a `TrustedDerivations` implementation (see the next section); without a reference key, fields that need references fail closed.
- **Values and cost.** Literals and `@name` analysis values become typed BigQuery parameters. Names starting with `_policy_` or `_value_` are reserved. The compiled query carries `maximum_bytes_billed` (default 1 GiB) for the executor to apply.
- **Postconditions.** Every remaining table must be a trusted physical source. Every parameter must be accounted for. The emitted SQL must reparse to a single `SELECT`.

Rejections are `QueryRejected` errors with a `ToolErrorCode`, a reason and a safe message. An unknown field and a forbidden field produce the same error.

Tests in `tests/unit/sql_compiler/` include allowed queries checked against a DuckDB result oracle, about 200 adversarial queries, and Hypothesis properties: changing hidden data does not change authorized results, and arbitrary input either fails safely or compiles to scoped SQL. `tests/live/test_sql_compiler_dry_run.py` dry-runs every allowed query in BigQuery, which costs nothing, when a project is configured.

### Customer privacy: references, age bands and the result boundary

The project's privacy interpretation: names, email addresses, street-level addresses, fine location and raw customer/order/item keys never reach the model. Customers, orders and items are explored through opaque references. Demographics (country, state, age band) are allowed for individual customers and for populations, with no minimum group size. Exact ages are never available. This is pseudonymization, not anonymization: demographic combinations can still single people out, and the project does not claim otherwise.

- **Opaque references** (`customer_ref`, `order_ref`, `item_ref`; `adapters/sql_compiler/derivations.py`) look like `cus_` plus 24 hex characters. BigQuery computes them inside the trusted binding as HMAC-SHA256 of `<kind>:<raw key>`, so raw keys never leave the warehouse. BigQuery has no HMAC function, so the RFC 2104 construction is written out with `SHA256` and the two padded keys, which are passed as secret trusted parameters. The key is derived per executive from the master key `RETAIL_ANALYTICS_REFERENCE_KEY`. One executive's references are stable across sessions and relations, so joins, follow-ups and saved reports keep working. Another executive's references for the same customer are unrelated, so they cannot be correlated across executives, and a reference pasted from another executive's results matches nothing. A lookup by reference is an ordinary filter in a compiled query, so it always runs under the current product scope. Without the key, references cannot be reversed or recomputed. Limits: BigQuery records query parameters in job metadata, so a principal who can read the project's jobs could compute one executive's references from raw keys (never the master key, and never reverse a reference). Rotating the master key retires every reference. If the key is unset, customer-level references are unavailable (queries fail closed), while age bands keep working.
- **Age bands** are cells of a fixed 5-year grid anchored at multiples of 5 (`25-29`), top-coded at `90+` (`domain/privacy.py`). Analysis chooses coarser bands by merging cells (`CASE WHEN age_band IN ('25-29', '30-34') ...`). It cannot get finer ones: raw age never appears in model SQL and the grid never moves, so shifted boundaries or differencing cannot reveal an exact age. A property test changes hidden ages within their cells and checks that no allowed query result changes.
- **Result boundary.** `ResultPrivacyBoundary.release(compiled, rows, catalog=<fresh view>)` (`application/result_privacy.py`) is the only way query rows reach the model, evidence or reports. It withholds the whole result (`ResultWithheld`) in any of these cases: the authorization or catalog version changed since compilation; an output's lineage uses a field that is no longer published or has a forbidden source; the result shape differs from the compiled outputs; a value is not a plain scalar; or a column that passes a reference or age band through holds anything other than a well-formed reference of that kind or a grid band. Free text that looks like an email, phone number or street address is masked and counted. Releases are bounded to 500 rows or 256 KiB, with truthful truncation flags. Secret parameters are hidden from `repr`, and `CompiledQuery.analysis_parameters` never includes them.

Tests: `tests/unit/privacy/` (end-to-end compiled queries over the DuckDB oracle with real derivations, adversarial identifier/age/cross-executive cases, boundary unit tests) and `tests/live/test_privacy_derivations_live.py` (BigQuery dry runs, plus one literal-only query that checks that BigQuery computes the same reference and bands as the application).

### Durable query execution

`QueryExecutionService.execute(QueryAttempt(...))` (`application/query_execution.py`) runs one attempt of one query operation. The BigQuery adapter is `adapters/bigquery/jobs.py`, behind the SDK-free `WarehouseQueryJobs` port (`application/warehouse_jobs.py`). Wiring: `bootstrap.query.build_query_execution(settings, persistence, resolver, discovery, budgets=...)`.

- **Fresh authority on every attempt.** Each attempt re-reads the executive's entitlements and catalog view and recompiles the model's query with them. Authority is read again right before rows pass `ResultPrivacyBoundary`. If access was revoked, nothing is released. If the scope changed while a job ran, that job is cancelled and its result is never used.
- **Reference before submission.** The job ID is derived from the operation ID and a submission number (`domain.executions.query_job_id`). It is recorded in `query_executions` before the job is submitted. Every job carries the compiled `maximum_bytes_billed` and a statement fingerprint label. A dry run comes first: it validates the statement and estimates its bytes, and an estimate over the cap stops the query before any job exists.
- **Reconcile first.** If a job reference exists, the attempt looks the job up before doing anything else. A lost submission response or a worker crash leads back to the same job. Resubmitting a recorded ID cannot create a second job, because BigQuery refuses the duplicate. A second job of the same operation is only submitted after the first one has ended with no result to release: a transient job failure, or authority that changed after submission.
- **Outcomes** are `QuerySucceeded` (released rows plus job statistics), `QueryPending` (JOB_PENDING: the job is still running after the attempt's bounded wait), `QueryOutcomeUnknown` (reconcile before anything else), `QueryFailed` (an error code with `retryable`/`correctable`), and `QueryCancelled`. `cancel(run_id, operation_id)` requests BigQuery cancellation and reconciles it. Failure details are a fixed vocabulary. SQL and parameter values are never logged or persisted by this path. The statement itself stays in BigQuery's job metadata.
- **Limits held by the operation record.** The operation gets a deadline when it is first recorded (`RETAIL_ANALYTICS_QUERY_DEADLINE_SECONDS`, default 120). Past it, the job is cancelled and reconciled. The operation then fails with `BUDGET_EXCEEDED`/`query_deadline`, and no result is released. While cancellation is still unconfirmed, `QueryFailed.stopping` names the job, and `reconcile_cancel` finishes it. The job also carries a BigQuery job timeout 30 seconds longer than the deadline, as a backstop if no worker is left. At most `RETAIL_ANALYTICS_MAX_TRANSIENT_ATTEMPTS` (3) attempts may end in a transient failure, and at most that many jobs are submitted. The last allowed failure fails the operation with `retries_exhausted` instead of asking for a retry. Both counts come from the persisted history, so a restart cannot reset them.
- **Run budgets** plug in through `QueryAdmission.admit(...)`. It is called with the dry-run estimate before each new submission is recorded. `QueryUsageRecorder.settle(...)` is called once a job has finished. Both are implemented by `RunBudgets` (see "Run budgets and recovery").
- The source tables are public, so these credentials can read them directly. The application query path is the enforcement boundary, not IAM on the rows.

Tests: `tests/unit/query_execution/` (fault injection with fakes: lost responses, crash after submit, slow jobs, missing jobs, quota and transient failures, revoked or changed access, cancellation; adapter request shape and error mapping) and `tests/live/test_query_execution_live.py` (tiny bounded queries, live reconciliation of a lost response, and a duplicate job ID refused by BigQuery).

### Run budgets and recovery

One investigation run has one persisted account (`run_budgets`, `budget_charges`; `domain/budgets.py`, `application/budgets.py`, `adapters/postgres/budgets.py`). Wiring: `bootstrap.budgets.build_run_budgets(settings, persistence.budgets)`, passed as `budgets=` to `build_query_execution`. Defaults (design section 39), all validated settings:

| Limit | Setting | Default |
| --- | --- | --- |
| Active time (clarification waits excluded) | `RETAIL_ANALYTICS_RUN_ACTIVE_SECONDS` | 600 |
| Provider requests, fallback included | `RETAIL_ANALYTICS_RUN_MAX_PROVIDER_REQUESTS` | 20 |
| Input + output tokens | `RETAIL_ANALYTICS_RUN_MAX_TOKENS` | 100000 |
| Query executions (job submissions) | `RETAIL_ANALYTICS_RUN_MAX_QUERIES` | 10 |
| Bytes per query / per run | `RETAIL_ANALYTICS_QUERY_MAX_BYTES` / `RETAIL_ANALYTICS_RUN_MAX_BYTES` | 1 GiB / 5 GiB |
| Reformulations per failed query | `RETAIL_ANALYTICS_QUERY_MAX_CORRECTIONS` | 2 |
| Attempts that may fail transiently | `RETAIL_ANALYTICS_MAX_TRANSIENT_ATTEMPTS` | 3 |
| Backoff base / cap (seconds) | `RETAIL_ANALYTICS_RETRY_BASE_SECONDS` / `RETAIL_ANALYTICS_RETRY_MAX_SECONDS` | 1 / 20 |
| Query deadline (seconds) | `RETAIL_ANALYTICS_QUERY_DEADLINE_SECONDS` | 120 |
| Rows / bytes per tool result | `RETAIL_ANALYTICS_RESULT_MAX_ROWS` / `RETAIL_ANALYTICS_RESULT_MAX_BYTES` | 500 / 256 KiB |

- **Pinned and never reset.** The limits are stored with the run when its account opens (`RunBudgets.open`, or lazily on the first charge). A resumed run, a restarted worker or a configuration change keeps the run's limits and its usage. New settings only apply to new runs.
- **Atomic, idempotent charges.** Each charge locks the run's budget row, so concurrent charges from any number of workers can never jointly pass a limit. Each charge is recorded once per (run, kind, key). A retried activity that repeats a charge gets the recorded charge back and is not counted again.
- **Queries.** Every job submission is one query, charged with its dry-run estimate before the job reference exists (key `<operation>#<submission>`). When the job finishes, the estimate is replaced by the billed bytes, or by the processed bytes when billing is not reported. A job that never reports usage keeps its estimate and is marked ambiguous. Real usage is recorded even when it is above a limit. Later charges are then refused.
- **Provider requests** (`ProviderBudget`, implemented by `RunBudgets`; the model runtime calls it). `reserve_provider_request(run_id, request_key, estimated_input_tokens=...)` is called before each request is sent, fallback requests included. `record_provider_usage(run_id, request_key, ProviderUsage(input_tokens, output_tokens))` is called afterwards. The reported input+output tokens replace the estimate. If the provider reports nothing, the estimate stands and the charge is marked ambiguous, so usage is never undercounted to zero.
- **Active time** is wall-clock time while the run is active. Waiting for the warehouse, backoff and model calls all count. Only a clarification wait is excluded: `pause_for_clarification` / `resume_after_clarification`.
- **Recovery** (`application/recovery.py`). `classify(outcome)` returns the next action. `WAIT`/`RECONCILE` mean a job may exist: check the recorded job and never submit again. `RETRY` is the same operation after `RunBudgets.retry_decision(run_id, failures)` allows it. The delay uses exponential backoff with equal jitter and honours a longer provider retry-after. `REFORMULATE` and `NARROW` are new operations, reserved with `reserve_correction(run_id, op, corrects=...)` (2 per chain). `EMPTY` is a valid, complete empty result. `STOP` means a run limit, access or an unrecoverable failure. `complete` is false whenever rows were cut, so a truncated result is never presented as the whole answer.
- Handlers see `ExecutionContext.budget`, a read-only snapshot (`RunBudgets.with_budget`). Budgets are never tool arguments.

Tests: `tests/unit/budgets/` (boundaries with a fake clock, settlement, pausing, backoff, settings, and the store contract run against an in-memory store), `tests/unit/query_execution/test_limits.py` (deadline cancellation and reconciliation, transient-attempt and submission caps, budgets through admission, truncation and empty results), and `tests/integration/test_budgets.py` (PostgreSQL: the same contract with concurrent reservations from separate connection pools, plus a restart with other settings).

### Evidence and guarded reuse

Evidence (`domain/evidence.py`, `application/evidence.py`) is the immutable supporting material behind a finding. Query evidence is built only from a released result and its compiled query (`EvidenceService.record_query`). It stores the released rows, the logical query, the model's analysis parameters (never the compiler's trusted or secret parameters), a digest of the executed statement (the statement itself stays with the query execution record), metric definition versions, catalog and privacy-policy versions, period and time zone, the analytical preference fingerprint, the grain (the columns the rows are grouped by), truncation and computation time. The authorization version and a digest of the exact product set come from the caller's freshly resolved context. Payloads are bounded JSONB in PostgreSQL (512 KiB of rows, 64 KiB of provenance; larger results belong in artifacts). Rows are append-only: an UPDATE trigger rejects changes, and a content digest is checked again whenever a record is read for reuse. A retried operation returns the record it already stored. A refresh stores the next version of the same lineage, so a report keeps citing the snapshot it used. Derived and external evidence (`EvidenceService.record`) name their inputs, which must be the caller's own current evidence.

Reuse is a privacy boundary. `EvidenceService.find_reusable(context, ReuseRequest)` loads candidates from the caller's own session only. `ReusePolicy` then checks each candidate in this order:

- **Authority:** same owner and session, a non-empty product scope, unchanged authorization version and product set, intact content, not invalidated. Any change to an executive's entitlements makes earlier evidence unusable, both for reuse and for context (`usable_in_session`).
- **Meaning:** catalog and policy versions, the required metric definition versions, preference fingerprint, period and time zone.
- **Sufficiency:** explicit refresh always queries again; questions about current data reuse only evidence computed within the freshness limit (`RETAIL_ANALYTICS_EVIDENCE_CURRENT_FRESHNESS_SECONDS`, default 900 = 15 minutes, allowed 60-86400; `build_evidence(persistence, settings=...)` applies it and `current_freshness=` overrides it in tests); explanations reuse the original snapshot at any age and disclose when it was computed; a requested breakdown must be in the grain; truncated rows cannot feed a calculation.

Reused evidence is linked to the new run in `run_evidence`. Changing a preference that affects computed numbers (a metric definition or time zone) marks dependent evidence invalidated, including evidence derived from it (`PostgresEvidenceStore.invalidate_dependent_findings`, wired as the preference service's invalidator). A saved report retains its evidence through pins (`pin_for` / `release_pins`). A pinned record cannot be deleted, but a pin never authorizes reading it. Investigation cleanup is not implemented yet.

Tests: `tests/unit/evidence/` (policy rules with a fake clock, encoding, recording from real compiled and released queries, two executives, version and entitlement changes, invalidation, pins) and `tests/integration/test_evidence.py` (PostgreSQL: immutability, idempotent concurrent recording, refresh versions, entitlement changes, preference invalidation, pin retention).

### Currency conversion

`convert_currency` (`capabilities/currency.py`, spec via `currency_capability(service)`; not yet registered in a runtime root) converts numeric amount columns of recorded evidence and records the result as derived evidence (`application/currency_conversion.py`). The original evidence is untouched; the derived record keeps the original columns, adds `<column>_<currency>` columns and names its input in `derived_from`. Provenance notes hold the source currency, rate, rate date, source, method, basis, requested date and rounding rule. Amounts round half up to the target currency's minor units.

- **Source currency** comes only from a `SourceCurrencyProvider`; the default is unknown (`SourceCurrency.unknown()`), which refuses every conversion. Currency is never inferred from prices.
- **Target** is the explicit `target_currency` argument, else the executive's saved `display_currency` preference (`StoredDisplayCurrency`).
- **Basis.** `current` uses the latest published rate; `historical` needs `as_of`. If the basis is omitted and the amounts cover a finished period, the tool refuses and asks for a choice, since the figures differ.
- **Refusals** (unknown source currency, unsupported pair or date, provider down, unclear basis, same currency) return an explanation, never an estimate.
- **Provider.** Public ECB euro reference rates through [Frankfurter](https://frankfurter.dev) v2 (`adapters/exchange_rates/frankfurter.py`): no API key and no quota (only abuse limiting), mid-market, daily on TARGET business days, non-euro pairs crossed through EUR, about 30 currencies. Requests pin `providers=ECB`, because without it Frankfurter blends 100+ central banks. A dated request returns the latest business day on or before it; the returned date is what is disclosed. Retries 429/5xx with backoff. `RETAIL_ANALYTICS_EXCHANGE_RATE_BASE_URL` points at another instance. Offline tests use `adapters/exchange_rates/fixture.py`. Wiring: `bootstrap.currency.build_currency_conversion(settings, evidence, preference_store, source=...)`.
- **Source currency is declared, not verified.** The dataset carries no currency metadata and it is never inferred from prices. `RETAIL_ANALYTICS_SOURCE_CURRENCY_DECLARED` (ISO 4217; `.env.example` and bootstrap default it to `USD` locally, never overwriting a non-empty value) lets the operator declare it. A declared currency is typed as declared (`SourceCurrency.declared`), and every conversion result, its provenance notes (`source_currency_basis`) and the user-facing disclosure say "source currency declared by operator, not verified from data". Empty means unknown and every conversion is refused. An invalid code fails startup with a configuration error naming the variable.

### Context selection and the output privacy gate

Stored history is larger than model context. It was also produced under whatever authority applied at the time. `ContextBuilder.build(principal, run_id, request, request_message_id=...)` (`application/context.py`) assembles one model iteration's context under the caller's *current* authority. It resolves authority again on every call, so call it on every attempt and iteration:

- **Evidence** comes from `EvidenceService.session_standing`. Only records usable now are included: same owner and session, unchanged authorization version and product set, intact, not invalidated, and not from before a topic reset. Records are newest first, with bounded rows. When space runs out, a record shrinks to a reference that can be fetched by ID.
- **History** passes `HistoryRules` (`domain/context.py`). If a run used evidence that current authority no longer allows, its assistant answer is withheld, and its user messages keep their words but lose their figures and references. If a run used invalidated evidence (a changed definition or preference), its answer is withheld as superseded. Messages are also judged by time, because a later message can repeat an earlier figure. After the first evidence computed under the current authority, everything was produced under that same authority, since authorization versions only increase. Anything older than that, but newer than evidence that is now withheld, is treated as possibly repeating it.
- **Every text** that enters context is screened. This covers the request, history, preferences and evidence cells. Direct personal data is masked, including names and contact details the user typed. References that current evidence does not contain are masked too. Untrusted text is quoted, so it cannot open or close a context block. Quoting preserves structure but is not the security boundary.
- **Budget:** `ContextBudget` (default about 8k tokens, 12 history messages, 6 evidence records with 20 rows each). The result says what was omitted and why, as counts only.
- **Topic reset:** `ContextBuilder.reset_topic(principal, session_id, reset_id)` records a boundary in `topic_resets` (migration 0010). It is owner-only and idempotent. Earlier messages and evidence leave context, but nothing is deleted: reports, pins and retained evidence stay, and evidence can still be explained by ID.
- **Request scope:** `assess_request(text, ongoing_investigation=...)` (`domain/request_scope.py`) declines clearly off-topic requests (creative writing, general knowledge, coding help, prompt-extraction attempts) before any model work. Analysis, report administration and preference administration proceed. A short follow-up during an investigation steers it. An ambiguous first message gets a focused clarification.

`OutputPrivacyGate` (`application/output_privacy.py`) checks every generated section before it is shown, streamed, saved or promoted. `check(principal, run_id, sections, destination)` builds a fresh `DisclosurePolicy` and releases all sections or none. `policy_for_run` plus `release` does the same one section at a time. It fails closed:

- Citations must name evidence the caller may use now.
- Opaque references must appear in that evidence, which rules out fabricated references, references from revoked scope and other executives' references.
- A figure that matches only evidence the caller lost access to is blocked. Small integers and years are not compared, and derived figures such as percentages of withheld values cannot be recognized.
- Trusted parameter names and key material are blocked.
- Direct personal data is masked in displayed answers and progress, and blocks report and memory content, which must be regenerated rather than saved with gaps. This covers names, contact details, street addresses, postal codes, coordinates, raw keys, exact ages, birth dates and identifiers hidden in encodings (base64, hex, percent, HTML entities, full-width or zero-width characters).
- An error inside the check withholds the section.

Demographics (country, state, age bands) and small groups are released; there is no minimum group size. Names have no general shape. They are found by context cues ("customer named ...", honorifics, a name next to a customer reference), and by exact match against protected terms: names the user typed, plus an optional trusted lexicon passed to `build_context(..., protected_terms=...)`. `redact_for_telemetry` and `screen_for_memory` apply the same detectors to traces and to anything stored beyond the session. These detectors are defense in depth. The compiler and the result boundary keep identifiers out of model input in the first place.

Wiring: `bootstrap.context.build_context(persistence, access, evidence, preferences)` returns `ContextServices(builder, gate, resets)`. Nothing is wired into the agent loop yet.

Tests: `tests/unit/context/` (detectors and encodings, scope narrowed mid-session, stale and tampered evidence, topic reset, budget, injection across user text, tool results and history, and positive demographic and administration cases) and `tests/integration/test_context.py` (PostgreSQL).

### Saved reports

`ReportService` (`application/reports.py`, wiring `bootstrap.reports.build_reports(persistence, artifacts, evidence, gate, resolver)`) saves, lists, reads, exports and searches an executive's reports. Tables (migration 0013): `reports` (owner, originating session, `deleted_at`), immutable `report_versions` (title, artifact version, product-set digest, idempotency key) and `report_evidence`; migration 0015 adds `product_scope_snapshots` and `report_required_scopes` (see below). The Markdown body is an artifact.

- **Save:** `create(principal, run_id, ReportDraft, operation_id=, report_id=None, base_version=None)`. A draft has a title, summary, findings (each cites at least one evidence ID), definitions, limitations and action items. Findings are observed results. Action items render under "Recommended actions" as recommendations, never as findings. Text may only mention evidence IDs the draft cites. The text goes through `OutputPrivacyGate` for destination REPORT, which resolves authority afresh immediately before the first write. If cited evidence became inaccessible the report is discarded (`OutputWithheld`); nothing is stored. Trusted code adds the data basis for each cited record: kind, period and time zone, definitions with the date field they are dated by (order dates are `orders.created_at`), truncation and source notes. Cited evidence is pinned for the report (`EvidenceService.pin_for`, holder `report`). Repeating an `operation_id` returns the original version; with different content it is a conflict. `base_version` guards against saving over a newer version. New versions keep earlier ones readable.
- **Reading pinned evidence:** a pin retains data and never authorizes reading it. A report version and its evidence are readable by the owner only, and only while the owner's current products cover the version's *required scope*: the union of the exact product sets its cited evidence was computed under. Those sets come from trusted execution metadata only: when evidence is recorded, the execution context's product set is stored as a scope snapshot keyed by the evidence's product-set digest (`product_scope_snapshots`, migration 0015; immutable, with a CHECK that ties the digest to the IDs), and saving a version records the union (`report_required_scopes`). They never come from model arguments (tool inputs reject extra fields) or from the products that appear in result rows. Widening access keeps old reports readable; removing a product the version does not require changes nothing; removing any required product makes `read` and `export` fail with `ACCESS_CHANGED`, listings and deletion previews show no title, and search skips the report. Saving a new version from a fresh analysis restores access. The subset check runs in PostgreSQL, so product IDs never reach reports, tool results or model context. Versions saved before migration 0015 get a required scope only when every cited evidence stamp is provably identical to some executive's current entitlements (same digest); the rest keep the strict rule (readable only while the owner's product set equals the one at save time). Authority is judged on every call; nothing is cached.
- **Definition notices:** query evidence records, as analytical context, the definitions relevant to the fields it read; this is not proof that the SQL implemented them. It comes from trusted compile-time data (`application.evidence.query_basis`): the catalog definitions whose population and measure fields the compiled query read, what "revenue" (and any term the executive defined) meant under the effective preferences, the compiler's exact date window (`CompiledQuery.date_window`, only when every dated read is bounded to the same window; otherwise not recorded), the date field and UTC. `read` and `export` compare it with the reader's current definitions (`resolve_term` over their effective preferences, plus the catalog's current versions; `session_id=` adds that conversation's preferences) and return display-time `DefinitionNotice`s naming both definitions and saying the figures were not recalculated. Evidence recorded without definitions gets a neutral "definitions used by this report were not recorded" notice, and its figures are never reused in another session (`definitions_unknown`): unknown is not compatible. Notices never change the saved Markdown or version and never block reading. HTTP returns them as `definition_notices` (read) and the `X-Report-Definition-Notices` header (export); the CLI and the `read_report`/`export_report` tools show them. Limits: definitions are attributed by the fields a query read, not by proving the SQL implements a definition (a query reading `sale_price` might compute an average, not revenue), so a notice or source line never certifies that a metric was applied. A query that compares several periods currently has no single recorded period (no explicit multi-period marker exists, and reuse rules are unchanged), so report authors and agent outputs should state the compared periods themselves.
- **Export:** the Markdown plus a table per cited evidence record. Missing product names and brands display as "Unnamed product" and "Unknown brand" next to the product ID; stored evidence keeps NULL.
- **Search:** title and content, owner's live reports only, newest 200 scanned, optional conversation filter. Deleted reports are excluded by the repository (see Report deletion below).
- **Limits:** one report cites at most 50 evidence records and 40 findings, and its Markdown is capped by the artifact limit. A new version must cite evidence usable in the current session. A crash between the artifact write and the report record can leave an unreferenced artifact version that is owner-only and never listed.

Tests: `tests/unit/reports/` and `tests/integration/test_reports.py` (PostgreSQL, concurrent duplicate saves, immutability, access change).


### Report deletion

`ReportDeletionService` (`application/report_deletion.py`, wiring `bootstrap.report_deletion.build_report_deletion(persistence, resolver)`, migration 0014: `deletion_proposals`, `deletion_proposal_items`, `audit_events`) deletes saved reports in two steps, and the model only takes the first.

- **Propose (model-facing):** the `propose_report_deletion` capability (`capabilities/report_deletion.py`, `report_deletion_capability(service)`; not registered in the agent loop yet) takes exact report IDs the model found through the owner-only report search or listing. `propose(ctx: OperationContext, report_ids)` freezes each owned live report at its current version, expires after 10 minutes, deletes nothing and returns titles, dates, count and expiry. Its arguments cannot carry approval (the registry rejects such names) and nothing in its output lets the model confirm. `operation_id` is the idempotency key, a retry returns the same proposal. At most 25 reports per proposal and 20 pending proposals per executive.
- **Confirm (application only):** `confirm(principal, proposal_id) -> DeletionResult`, `cancel(principal, proposal_id)`, `preview(principal, proposal_id)`, `list_pending(principal)` (own pending, unexpired proposals, newest first; behind `GET /v1/deletion-proposals?status=pending`). With `progress=` wired (the agent runtime does), `propose` also publishes a `deletion.proposed` run event carrying only the proposal ID. Call `confirm` only from an authenticated user's explicit action (the CLI `confirm <proposal-id>`), never from model output. One transaction locks the proposal and its reports (the same per-report advisory lock that saving a version takes), rechecks requester, status, expiry, ownership and versions, soft-deletes all of them (`reports.deleted_at`), consumes the proposal and appends an `audit_events` row. Any failure, including the audit insert, changes nothing. Failures: `AccessDenied` (unknown proposal or another principal's, no `reports:delete_own`), `DeletionError` with code `expired`, `already_resolved` (replay or concurrent second confirmation) or `stale` (a proposed report was revised, deleted or is no longer the requester's). Reports created after the proposal are never included, and a stale proposal needs a fresh one.
- **Reuse stops at deletion:** the same transaction withdraws every link the deleted reports gave the owner's other sessions (`session_report_evidence.withdrawn_at`, migration 0018; one link per report the evidence was reached through) and records the count in the confirmation's audit event (`reuse_links_withdrawn`). Evidence reached only through withdrawn links, and session evidence derived from it (`evidence_source_withdrawn`), is then withheld like evidence current access no longer allows (`report_link_withdrawn`): not in model context, not citable through the output gate, not returned by `fetch_evidence` or `find_reusable`, and earlier answers that used it leave model context. Evidence the session computed itself, or still linked through another live report, is unaffected. A report read just before the deletion cannot be imported after it (the import checks the report under its row lock).
- **Audit:** `adapters/postgres/audit.append_audit(connection, AuditEvent)` appends inside the caller's transaction. Events hold identifiers, versions and counts, never titles or content.
- **Recovery:** `DeletionResult.recoverable_until` is seven days after deletion (`domain.report_deletion.RECOVERY_PERIOD`). Restore and purge are described under "Report recovery and lifecycle cleanup"; there is no agent restore tool.
- **Titles are untrusted text:** the preview flattens control characters and limits a title to 200 characters, and withholds it (`None`) when the owner's product access changed since the report was saved, though such a report can still be deleted.

### Report recovery and lifecycle cleanup

`LifecycleService` (`application/lifecycle.py`, wiring `bootstrap.lifecycle.build_lifecycle(persistence, resolver, artifacts, settings=settings)`, store `adapters/postgres/lifecycle.py`, rules in `domain/lifecycle.py`; no migration). Nothing here is a model capability: the agent has no restore tool and no tool reaches `LifecycleService`.

**Operator commands** (`retail-analytics-maintenance`, or `python -m retail_analytics.bootstrap.maintenance`; after pulling a change that adds the script run `pip install --no-deps -e .`). They run with the backend settings against its database, so they are for trusted operators, and print identifiers, dates and counts only, never titles or content.

```sh
retail-analytics-maintenance cleanup --dry-run               # counts only, changes nothing
retail-analytics-maintenance cleanup [--max-reports N --max-sessions N --max-audit-rows N]
retail-analytics-maintenance list-restorable --as <executive-id>
retail-analytics-maintenance restore <report-id> --as <executive-id>
retail-analytics-maintenance unresolved                      # operations flagged for manual resolution
```

`cleanup` is the scheduled job: run it from cron or any scheduler (for example hourly). It is idempotent and bounded, so overlapping or repeated runs are safe, and `more_pending=true` in its output means a bound stopped it early. No bootstrap step is needed: a fresh environment has nothing to seed. Settings: `RETAIL_ANALYTICS_AUDIT_RETENTION_DAYS` (90) and `RETAIL_ANALYTICS_CLEANUP_BATCH_SIZE` (100). Closed Temporal histories keep seven days through the namespace retention in `compose.yaml`. No custom retention exists for local telemetry.

- **Restore** `LifecycleService.restore(principal, report_id)` (what T21/T22 call with an authenticated principal; `list_restorable(principal)` finds IDs). Allowed for the owner holding `reports:delete_own` or a principal with `access:admin`; anyone else, and unknown IDs, get the same `not_found`. It works strictly before `deleted_at + 7 days`, only if the report was not purged, and only while the owner is an active executive; otherwise `RestoreError` with code `not_deleted`, `window_closed`, `purged` or `owner_unavailable`. It locks the report row, rechecks, clears `deleted_at` and appends a `report.restored` audit event in one transaction. The consumed deletion confirmation stays consumed. Restoring never revives reuse by itself: the links the deletion withdrew are marked `revalidation_pending` in the same transaction, then (`build_lifecycle(..., reuse=reports)`, wired in the maintenance CLI) `ReportService.revalidate_restored` checks each one again for the owner: the report version is still readable (required-scope coverage), the owner's products cover the record, the record is intact and not invalidated, and its meaning matches current definitions and that session's settings (unknown definitions are not compatible). Passing links are reinstated; the rest stay withdrawn (`revalidation_failed` with the first failed rule); a `report.reuse_revalidated` audit event records the counts. Reading the report again in a session re-runs the import checks and can reinstate a link (`reimported`).
- **Purge** of reports past the deadline: (1) one transaction removes versions, citations, evidence pins and proposal items, leaving a content-free tombstone row; (2) `ArtifactMaintenance.purge` deletes the artifact (metadata, then bytes); (3) one transaction deletes the tombstone and appends `report.purged`. Restore and purge both lock the report row, so one wins; a crash or storage failure leaves a tombstone that the next run finishes, and a tombstone cannot be restored. A report whose runs still have unresolved operations is skipped until they settle.
- **Investigations** expire seven days after the later of the last interaction and last run completion. Cleanup deletes their messages, progress, inputs, budgets and unpinned evidence. Evidence a saved report pins or cites (including soft-deleted reports, until purge), and what that evidence derives from, survives together with the bare session, run and operation rows it references. Sessions with active runs, or with unresolved operations (external jobs or deliveries submitted and not finished, or an unknown outcome), are left alone. Unresolved operations older than 24 hours get one `maintenance.unresolved_flagged` audit event and show in `unresolved` for manual resolution instead of being polled.
- **Pin races:** cleanup locks the evidence it may delete and then reads pins, so a pin committed first keeps the evidence; a pin attempted after fails (`AccessDenied`) instead of pointing at deleted evidence.
- **Audit retention:** audit events older than the configured days are deleted in batches. Ordinary report deletion never touches Golden Knowledge, which has its own lifecycle.
- **Limits:** this deletes database rows and artifact files only. Database backups, filesystem snapshots, WAL and copies already exported by a user are not erased and need their own retention policy before physical erasure is claimed. Reports cited by Golden provenance are not examined. `reports.session_id` may point at a cleaned-up session. Restore through the CLI is operator-run; a user-facing recovery UI is not built.

Tests: `tests/unit/lifecycle/` (rules, authorization, cleanup order, bounds, dry run) and `tests/integration/test_lifecycle.py` (PostgreSQL: restore, deadline, restore-vs-purge and purge-vs-pin races, storage failure, shells, unresolved operations, audit retention, Golden independence).

### Persona management

The company persona is free text for tone, level of detail, layout and terminology, managed without redeploying: draft, preview, publish, roll back (design section 21). It is presentation only. Permissions, the tool catalog, product scope, shared metric definitions and the required calculation, evidence and uncertainty disclosures stay in code and reviewed definitions; nothing in the persona reaches them, and the instruction block says so to the model (`domain/persona.py`, `application/persona.py`).

- **Authority:** every operation needs `persona:edit`, checked against the executive's current server-side roles (token scopes can only narrow it). Editors edit and discard only their own drafts; any editor may preview or publish a previewed draft. Drafts and versions are visible to editors only.
- **Screening:** personal data is refused and never stored. Text that tries to grant access, add tools, redefine metrics, alter figures, suppress limitations/evidence/uncertainty, bypass confirmations or limits, reveal internals, hide instructions in encodings or add links is stored as a draft with `findings` (rule names, never the matched text) but cannot be previewed or published. It is a heuristic in English plus a few Spanish phrases, not proof: authorization, data filtering, PII protection and confirmation are enforced in code regardless. Reserved tag-like markup (`</persona>`, full-width or invisible-character look-alikes, comments) is neutralized to a harmless marker such as `[policy]` on insertion; that protects structure only.
- **Flow:** `create draft` (based on the active version) -> `preview` (current and proposed persona over the same fixed sample findings; the draft is marked previewed only if every figure, evidence id and limitation survived; changes nothing for anyone else) -> `publish` with `expected_current` (one transaction: locks the active pointer, requires a preview of this exact text, a draft based on the active version, an unchanged active version; freezes the version, moves the pointer, writes history and audit). Concurrent publishes have one winner and the loser gets `conflict`. `rollback` makes an earlier published version active again with the same check; the history keeps both.
- **Runs:** a run pins the active version when it starts (`run_personas`, immutable) and renders it between the policy and the per-run context (`InvestigationRuntime`); publishing or rolling back affects only runs that start later. A user's own preferences still override persona defaults for presentation.
- **Audit:** `persona.draft_created|draft_updated|draft_discarded|previewed|published|rolled_back` commit with the change; refused publishes and rollbacks are recorded as `persona.publish_rejected|rollback_rejected` with the reason. Details hold ids, numbers and a content digest, never the text.
- **Interfaces:** CLI `retail-analytics-persona` (`show|history|check|draft|edit|preview|publish|rollback|discard`, `--as <executive-id>`; after pulling run `pip install --no-deps -e .`) and HTTP `/v1/persona/...` (docs/http-api.md). No bootstrap step: with no published persona runs get no persona block.
- **Limits:** the preview uses an offline renderer that lays out the sample findings and shows the exact instruction block; it cannot apply free-text style (`persona_applied=false`). A model-backed renderer implements `PreviewRenderer` and is not wired. Assistant-proposed drafts (a model tool) are not built. Migration 0016.

Tests: `tests/unit/persona/` (adversarial screening, markup, service gates), `tests/unit/http/test_persona_api.py`, `tests/integration/test_persona.py` (PostgreSQL: roles, concurrent publication, pinning, rollback, immutability, audit).

### Missing product labels

A few source products have no name or brand. Evidence keeps the NULL. Anything shown to the model or a person substitutes an explicit label (`domain/labels.py`): "Unnamed product" for a missing name and "Unknown brand" for a missing brand, applied only to NULL cells in columns that come from the product name or brand fields. The product ID is never replaced, and no fallback is used to group, deduplicate or join, so two unnamed products stay two rows with different IDs, and a brand breakdown keeps its NULL brand as its own "Unknown brand" group that reconciles with the total. Model context (`ContextBuilder`) and report exports add a note with how many rows use a fallback, and warn when a table has unnamed products but no product ID column (a query grouped by name alone merges them in the source query, so group by product ID). Tests: `tests/unit/context/test_label_fallbacks.py`.

### Local telemetry (MLflow, Prometheus, Grafana)

The same `compose.yaml` adds MLflow 3.14 (sanitized agent traces), Prometheus 3.12 (metrics) and Grafana 12.4 (dashboards), image versions pinned. For local development only: every port is bound to `127.0.0.1`, passwords are throwaway defaults, and none of it is a hardened or authenticated production setup (production hosting needs its own authentication, network restrictions, retention and backup design). No project-specific retention is configured; backend defaults apply.

```sh
docker compose up -d --build --wait postgres mlflow prometheus grafana   # same project as above
docker compose down                                                       # keeps volumes; -v deletes data
```

| What | Value |
| --- | --- |
| MLflow UI/API | `http://127.0.0.1:55500` (`COMPOSE_MLFLOW_PORT`); OTLP traces at `/v1/traces` |
| Prometheus | `http://127.0.0.1:59090` (`COMPOSE_PROMETHEUS_PORT`); OTLP metrics at `/api/v1/otlp/v1/metrics` |
| Grafana | `http://127.0.0.1:53000` (`COMPOSE_GRAFANA_PORT`); user `admin`, password `COMPOSE_GRAFANA_ADMIN_PASSWORD` (default `local-only-grafana`) |
| MLflow backend | database and role `mlflow` on the shared PostgreSQL (`COMPOSE_MLFLOW_DB_PASSWORD`, default `local-only-mlflow`); created idempotently by the one-shot `mlflow-db-init` service, so it also works on an existing `postgres-data` volume |
| Volumes | `mlflow-artifacts`, `prometheus-data`, `grafana-data` |

MLflow uses its own database and role: the application and Temporal roles cannot connect to it, and it cannot connect to theirs. The Grafana Prometheus data source (uid `prometheus`) and the "Local telemetry overview" dashboard are provisioned from `docker/grafana/`. The API and worker push their metrics to Prometheus's OTLP receiver (no scrape job). The MLflow image is built from `docker/mlflow/Dockerfile` (upstream image plus the pinned PostgreSQL driver).

The `docker` tests in `tests/integration/test_telemetry_stack.py` push a synthetic metric (OTLP) and a sanitized sample trace, restart the services and check both are still readable, directly and through Grafana.

#### Application traces, metrics and the "Agent overview" dashboard

Telemetry is on by default (`RETAIL_ANALYTICS_TELEMETRY_ENABLED=true` in `.env.example` and in the settings default); `./scripts/bootstrap.sh` and `./scripts/dev.sh` start the stack above. Set the variable to `false` (or pass `--no-telemetry` to bootstrap on a new env file) to opt out. Tests and `./scripts/check.sh` force it off and make no network calls. When on, the API and worker export:

- **Traces to MLflow** (OTLP/HTTP protobuf): all spans of a run share one trace (`tr-` plus an id derived from the run id), covering API acceptance, tool attempts, query attempts (BigQuery job id, bytes), retrieval, model attempts (provider, model id, attempt number, fallback from/to and reason class) and a run root span that names the provider that produced the final answer. `python -m retail_analytics.bootstrap.trace_lookup <run_id> [--tree]` prints the trace id, links and the span tree.
- **Metrics to Prometheus**, shown on the provisioned Grafana dashboard "Agent overview": runs and latency, budget use, query bytes, provider/fallback rates and final-answer provider, gate withholds, compiler rejections by class and exception type, retrieval hit/no-match, tool failures.

Telemetry is best effort: bounded queues and short timeouts drop data when MLflow or Prometheus is down and never delay a run; mutation audits stay in PostgreSQL. Spans and metrics hold identifiers, codes and sizes only (no prompts, SQL, rows, personal data or credentials). See [docs/observability.md](docs/observability.md).

### Package layout and dependency rules

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

### Dependencies

Direct dependencies are pinned exactly in `pyproject.toml`, one per line, sorted by name (`dependencies` for runtime, the `dev` extra for tooling). `requirements.txt` is the fully pinned, cross-platform lock generated from them:

```sh
./scripts/lock.sh    # uv pip compile requirements.in --universal --python-version 3.12 --no-annotate
```

Rerun it after changing any pin and commit both files. On a merge conflict in `requirements.txt`, resolve `pyproject.toml` first and regenerate rather than hand-merging.

### Technology choices

- **FastAPI and Uvicorn** for the HTTP/SSE backend: Pydantic-native request and response models, consistent with the Pydantic AI stack, and streaming responses for server-sent events.
- **Click** for the CLI: small, typed and widely maintained (Uvicorn already depends on it), with a built-in runner for command tests.
- **httpx** for the CLI's HTTP client: synchronous and asynchronous APIs and an in-process mock transport for tests.
- **Alembic with SQLAlchemy and psycopg 3** for PostgreSQL migrations: versioned, reviewable migrations with upgrade/downgrade and offline SQL generation. Domain records stay independent of SQLAlchemy.
- **python-dotenv with Pydantic models** for configuration: an explicit loader whose validation errors never include secret values.
- **Pydantic AI, the Temporal Python SDK, SQLGlot and google-cloud-bigquery** for the agent, durable execution, SQL compilation and BigQuery access.
- **PyJWT** for token signing and validation: small, maintained, explicit algorithm allow-lists and required-claim checks; the same library verifies identity-provider keys (JWKS) later.
- **Ruff, mypy (strict) and pytest** for checks.
