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

Migrations live in `migrations/` (Alembic, configured by `alembic.ini`; the URL comes from `RETAIL_ANALYTICS_DATABASE_URL` or `.env`). The baseline revision only creates `app_meta`; add new revisions with `alembic revision -m "..."` chained after the current head.

Docker-dependent tests carry the `docker` marker and are excluded from `./scripts/check.sh`. Run them with `python -m pytest -m docker`; they start their own uniquely named Compose project on free ports and remove it afterwards.

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
- **Ruff, mypy (strict) and pytest** for checks.
