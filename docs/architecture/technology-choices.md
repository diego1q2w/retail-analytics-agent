# Technology choices

Why each cloud service, model and framework was chosen, and what each choice
costs. Exact versions are pinned in `pyproject.toml`, `requirements.txt` and
`compose.yaml`.

## Cloud

| Choice | Status | Reason |
| --- | --- | --- |
| BigQuery | Implemented (required source) | The source data is `bigquery-public-data.thelook_ecommerce`. Dry runs validate statements and estimate bytes for free, `maximum_bytes_billed` caps every job, and client-chosen job IDs make lost submissions recoverable. |
| Google Cloud for production | Proposed | BigQuery and Gemini already live there, so data and model traffic stay inside one provider's network and one IAM model. Details: [production deployment](production-deployment.md). |
| Local Docker Compose | Implemented | One command runs PostgreSQL, the optional Temporal server and the telemetry stack on a laptop, with pinned image digests. |

## Models

| Role | Model | Status | Reason |
| --- | --- | --- | --- |
| Primary agent model | Gemini 3.8 Flash, through the Gemini Interactions API | Implemented | A newer Gemini model, as the brief prefers. Google [describes it](https://ai.google.dev/gemini-api/docs/models) as engineered for "autonomous agents, and complex enterprise workflows", which fits a multi-step tool-calling loop. With this project's key, it answered only on the Interactions API, so the project contains a small Pydantic AI model adapter for that API. |
| Backup agent model | GPT-5 mini, through the OpenAI Responses API | Implemented | A different vendor, so one provider's outage does not stop analysis. It supports function calling, streaming and structured output, and is [low cost](https://developers.openai.com/api/docs/models/gpt-5-mini). It is enabled only when a key is configured. |
| Embeddings | `gemini-embedding-2` (768 dimensions); an offline hashing embedder for fixtures | Implemented | Measured on the [retrieval benchmark](../../evaluation/retrieval/README.md). The thresholds are tuned for this model. |

The model can be changed in configuration. The application owns routing,
retries, cooldown and budget accounting, so the model cannot choose a
provider or loosen a policy. See [model providers](../model-providers.md).

## Frameworks and libraries

| Choice | Role | Why | Alternatives considered |
| --- | --- | --- | --- |
| **Pydantic AI** | Agent loop, tool calling, model abstraction | Typed tool inputs and outputs, native Gemini/OpenAI integrations and fallback, and a Temporal integration, without imposing a fixed graph. The agent is one adaptive loop over a small catalog of tools. | LangGraph (capable; its own checkpointing would overlap with Temporal), a custom loop (more model-protocol code to own), LiteLLM (not needed for two native providers) |
| **Temporal** | Durable execution (production; opt-in locally) | Durable history, timers, signals and activity retries for runs that must survive disconnects and crashes, wait for clarification and avoid duplicate external effects. See [why Temporal](production-deployment.md#why-temporal-and-the-simpler-alternative). | PostgreSQL job workers with leases (rebuilding recovery), CLI-owned execution (dies with the client) |
| **Local in-process manager** | Default local execution | The minimum prototype does not need durable replay. Investigations run as asyncio tasks in the API, on the same application steps and guards. | — |
| **PostgreSQL** | All application state | Transactions and row locks for exact deletion, budgets and evidence immutability. JSONB for bounded evidence. One engine in development and production. | SQLite (different concurrency semantics) |
| **SQLGlot** | SQL compiler | Parses BigQuery SQL into a syntax tree, so the compiler can allowlist node types, resolve scopes (CTE shadowing, subqueries) and rewrite relations into trusted projections. String or regex checks cannot do this safely. | View per executive (does not scale), regex checks (unsafe) |
| **FastAPI + Uvicorn** | HTTP and SSE API | Pydantic-native request and response models, streaming responses, generated OpenAPI. | — |
| **Click + httpx** | CLI | Small and typed, with a test runner. The CLI holds no backend credentials. | — |
| **OpenTelemetry → MLflow, Prometheus, Grafana** | Traces, metrics, dashboards | Vendor-neutral export through one sanitizing facade. MLflow shows agent traces, and Prometheus with Grafana covers metrics and the "Agent overview" dashboard. All of it runs locally with no account. | Hosted tracing services (need an account and data agreements) |
| **Alembic + SQLAlchemy Core** | Migrations, queries | Reviewable, linear migrations. Domain records stay independent of the ORM. | — |

## Author's experience with the chosen frameworks

**Not yet provided by the author.** The assignment asks for the author's
level of experience with the chosen framework. That statement must come from
the author and will be added here. It is not inferred from this repository.

## Costs of these choices

- Temporal adds a service, deterministic workflow code and versioning
  discipline. Local execution avoids that cost for the prototype but does
  not survive a process crash.
- The custom Gemini adapter must be maintained until Pydantic AI supports the
  Interactions API.
- SQLGlot's grammar coverage is restricted on purpose. Window functions,
  set operations and `UNNEST` are rejected, so some analyses need several
  simpler queries.
- Two model vendors double the data-processing agreements to manage.
