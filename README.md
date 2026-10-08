# Retail Analytics Agent

A conversational analytics assistant for retail executives. It investigates business questions using BigQuery data and curated analyst knowledge, supports follow-up exploration, and produces reports with evidence and action items.

## Project status

Early implementation. The application skeleton, configuration validation and repository checks exist; analytical behavior does not yet. The intended application uses a CLI connected to an HTTP backend, Pydantic AI for the agent, Temporal for durable execution, and PostgreSQL for application state. BigQuery provides read-only retail analysis; model and database credentials stay on the backend.

Setup for live services, public architecture documentation and evaluation results will be added as their implementations are verified.

Do not commit credentials, raw query results or private conversation data.

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
| `analytics` | `retail_analytics.bootstrap.cli` | CLI client; talks to the backend over HTTP only (`analytics status`) |
| `retail-analytics-api` | `retail_analytics.bootstrap.api` | HTTP backend (`GET /healthz`) |
| `retail-analytics-check-credentials` | `retail_analytics.bootstrap.check_credentials` | Verify BigQuery and Gemini access without printing secrets; see [Google access setup](docs/google-access.md) |
| `retail-analytics-worker` | `retail_analytics.bootstrap.worker` | Temporal worker (no workflows registered yet) |
| `retail-analytics-dev-access` | `retail_analytics.bootstrap.dev_access` | Development only: provision the two synthetic executives and issue local tokens; see [Authentication and entitlements](#authentication-and-entitlements) |

Backend entry points accept `--check-config`: validate settings, print them with secrets shown only as `<set>`/`<unset>`, and exit. Invalid configuration exits with status 2.

### Configuration

Copy `.env.example` to `.env` (ignored by Git). Process environment variables override `.env`; empty values count as unset. Backend settings use the `RETAIL_ANALYTICS_` prefix and the CLI uses `ANALYTICS_CLI_`. Unknown prefixed variables are rejected by name to catch typos.

- `RETAIL_ANALYTICS_MODE=fixture` (default) runs offline with no credentials.
- `RETAIL_ANALYTICS_GEMINI_MODEL` is the default model name used by the credential check.
- `RETAIL_ANALYTICS_MODE=live` requires the database URL, Temporal address, BigQuery project and Gemini API key; all missing settings are reported together.

Errors name the variable and the problem, never the value. All settings are declared in `src/retail_analytics/bootstrap/config.py`; add new ones there and to `.env.example` (a test keeps them in sync). Only bootstrap reads configuration; inner layers receive typed values.

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

### Local services (PostgreSQL and Temporal)

`compose.yaml` runs one PostgreSQL 17 server and Temporal 1.32 (image digests pinned), both bound to loopback only. Requires Docker with Compose v2. Passwords are throwaway local defaults; override with `COMPOSE_APP_DB_PASSWORD`, `COMPOSE_TEMPORAL_DB_PASSWORD`, `COMPOSE_PG_ADMIN_PASSWORD`.

```sh
docker compose up -d --wait postgres temporal      # project retail-analytics-local
docker compose run --rm temporal-namespace         # create the namespace (7-day closed-history retention)
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

Application state is reached through the narrow ports in `retail_analytics.application.persistence`, implemented with SQLAlchemy Core in `retail_analytics.adapters.postgres` and wired by `retail_analytics.bootstrap.persistence`. Retried writes are idempotent on application-generated keys (the operation ID for tool executions, the submission key for runs); reusing a key for different content raises a typed conflict. A session has at most one active run, enforced by a row lock and a partial unique index. Run events carry a gap-free per-run sequence for replay after a client's last received event ID.

Docker-dependent tests carry the `docker` marker and are excluded from `./scripts/check.sh`. Run them with `python -m pytest -m docker`; they start their own uniquely named Compose project on free ports and remove it afterwards.

### Authentication and entitlements

Assistant users are executives, distinct from the customers in the dataset. Three things are kept apart:

- **Identity**: a validated bearer token (JWT) names the caller by issuer and subject, mapped to one active row in `executives`.
- **Operation permissions**: server-assigned roles grant permissions (`executive`: `analysis:read`, `reports:read_own`, `reports:delete_own`; `editor`: `persona:edit`; `reviewer`: `knowledge:review`; `admin`: `access:admin`). The token's `scope` claim can only narrow them: effective permissions are the intersection, so a token cannot add a permission the server did not grant. `admin` manages access and grants no product data.
- **Product entitlements**: the complete set of product IDs whose data an executive may see, stored in `product_entitlements`. No rows means no product data, never unrestricted access.

Every change to an executive's roles, products or active status increments `authorization_version` in the same transaction. Tools never receive identity or entitlements as arguments: `AccessResolver.context_for_run(principal, run_id)` (`retail_analytics.application.authorization`) checks that the run and its session belong to the caller, reloads current authority, and returns the `ExecutionContext` whose `ProductScope` carries the products and version. Call it again for every attempt, including inside retried activities, so an entitlement change applies to the next tool check. `OwnershipGuard` loads sessions, runs and operations only for their owner, and `require_owner` applies the same rule to other owned records. A record owned by someone else is indistinguishable from a missing one.

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

Ten project-authored, reviewed example trios (question, SQL, report) live in `retail_analytics.application.golden_seed_library`. After the demo executives exist, `python -m retail_analytics.bootstrap.seed_knowledge` loads them through the normal submit and review lifecycle (author `demo-a`, reviewer `demo-b`) and is safe to rerun. See [docs/golden-seeds.md](docs/golden-seeds.md) for the corpus, validation and the manual review checklist.

### Schema discovery and metadata caching

The model sees a reviewed logical catalog (`domain/logical_catalog.py`, versioned), never raw warehouse metadata. Each logical field is an allowlisted mapping to named source columns; a source column that no reviewed field maps (for example a newly added email-like column) stays unpublished. Direct identifiers cannot back any field, raw keys only become opaque references and exact age only an age band; the catalog rejects such definitions at construction.

`list_relations` and `describe_relation` (`capabilities/discovery.py`) render the same `CatalogView` that SQL validation consumes (`DiscoveryService.view_for(context)` in `application/discovery.py`), so shown fields and types are the usable ones.

- **Source schema cache.** One shared cache of column names and types (metadata only, no rows). It refreshes after `RETAIL_ANALYTICS_SCHEMA_REFRESH_SECONDS` (default 3600: the public dataset's schema changes rarely, and metadata reads are free) and on demand through `invalidate()` after a schema-mismatch error.
- **Per-executive filtering is never cached.** Each call builds the view from the freshly resolved `ExecutionContext`; no permission or an empty product scope gives an empty view, so entitlement changes apply immediately despite a warm cache. Results carry the catalog and entitlement versions.
- **Drift.** A missing column or table, or an incompatible type, disables the affected logical fields (the whole relation if the field is essential, such as a key) and any joins that need them. Other fields keep working. Errors name logical relations only, never warehouse tables or columns.
- **Outage.** If a refresh fails, the last validated snapshot is served (flagged stale) for up to 24 hours, retrying at most every 30 seconds; after that discovery fails closed with a temporary-failure error.

`tests/live/test_schema_metadata.py` checks the catalog against live table metadata when a project is configured.

### Golden retrieval

`bootstrap.retrieval.build_retrieval` returns a `GoldenRetriever`: eligibility prefilter on index entries (product scope, schema/metric applicability) before any scoring, then BM25 and vector channels fused by reciprocal rank (k=60), at most 3 examples, none when no channel clears its threshold. Delivery goes only through `GoldenKnowledgeReader.deliver`, so stale index entries (retired, suspended, erased, changed) are refused and the next-ranked candidate is used. The index is in-process and rebuilt from `KnowledgeIndexSource` when the invalidation feed moves; vectors are cached by content digest. pgvector is not used: the pinned `postgres:17.11-alpine` image does not ship it and the corpus is tens of examples, so exact brute-force cosine is enough. Embeddings sit behind the `TextEmbedder` port: `hashing` (offline, deterministic, lexical only; the fixture default) or `gemini` (`gemini-embedding-2`, free-tier retries with backoff). Settings: `RETAIL_ANALYTICS_EMBEDDING_*` and `RETAIL_ANALYTICS_RETRIEVAL_*`. Thresholds are placeholders until T36 measures precision/recall.

### Restricted SQL compiler

The model never submits SQL for execution. It writes analytical SQL over the logical relations, and `SqlglotQueryCompiler` (`adapters/sql_compiler/`, behind the `QueryCompiler` port in `application/query_compiler.py`) compiles it against the executive's current `CatalogView` and `ProductScope`. Any query the compiler cannot fully understand is rejected.

- **Grammar.** Exactly one `SELECT`. Every SQLGlot node type and populated argument must be on an explicit allowlist (`grammar.py`). Writes, scripts, exports, set operations, window functions, `UNNEST`, system variables, unknown or user-defined functions and wildcard projections are rejected. `COUNT(*)` is allowed. Comments are stripped.
- **Resolution.** Sources are resolved by lexical scope, so a CTE or subquery named `customers` shadows the relation rather than binding to it. Every leaf must be a relation in the view, and every column must resolve to a published field or a derived output. Only an `ORDER BY` output alias may remain unqualified, which closes the unresolved-`HAVING` gap that SQLGlot qualification alone leaves open. The grammar is checked again after qualification (for example, a bare table alias that becomes a whole-row reference is rejected). Correlated subqueries, unused CTEs and duplicate output names are rejected.
- **Joins.** Only joins the catalog declares: a single equality from a relation already in the query to a new relation, `INNER` or `LEFT`, each relation at most once per query level. Joins through derived queries are rejected.
- **Binding.** Each logical relation is replaced by a trusted projection of only the referenced fields. The product scope (`@_policy_product_ids`) is applied at every physical read, before the model's query aggregates anything. Orders count only permitted items, and customers are reached only through permitted items. An empty scope is rejected outright. Opaque references and age bands come from a `TrustedDerivations` implementation; until one is configured, fields that need them fail closed.
- **Values and cost.** Literals and `@name` analysis values become typed BigQuery parameters. Names starting with `_policy_` or `_value_` are reserved. The compiled query carries `maximum_bytes_billed` (default 1 GiB) for the executor to apply.
- **Postconditions.** Every remaining table must be a trusted physical source. Every parameter must be accounted for. The emitted SQL must reparse to a single `SELECT`.

Rejections are `QueryRejected` errors with a `ToolErrorCode`, a reason and a safe message. An unknown field and a forbidden field produce the same error.

Tests in `tests/unit/sql_compiler/` include allowed queries checked against a DuckDB result oracle, about 200 adversarial queries, and Hypothesis properties: changing hidden data does not change authorized results, and arbitrary input either fails safely or compiles to scoped SQL. `tests/live/test_sql_compiler_dry_run.py` dry-runs every allowed query in BigQuery, which costs nothing, when a project is configured.

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

MLflow uses its own database and role: the application and Temporal roles cannot connect to it, and it cannot connect to theirs. The Grafana Prometheus data source (uid `prometheus`) and the "Local telemetry overview" dashboard are provisioned from `docker/grafana/`. Prometheus scrapes only itself for now; add the application job in `docker/prometheus/prometheus.yml` when metrics are exposed. The MLflow image is built from `docker/mlflow/Dockerfile` (upstream image plus the pinned PostgreSQL driver).

The `docker` tests in `tests/integration/test_telemetry_stack.py` push a synthetic metric (OTLP) and a sanitized sample trace, restart the services and check both are still readable, directly and through Grafana.

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
