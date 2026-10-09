# Technology choices

Each choice below follows the same order: what the project needs, the
options considered, the choice, what it costs, and the evidence and limits
behind it. "Implemented" choices are in the code; "proposed" ones are part of
the [production proposal](production-deployment.md) and are not final.
Exact versions are pinned in `pyproject.toml`, `requirements.txt` and
`compose.yaml`. Vendor statements link to the vendor's documentation, read on
2026-10-09.

## Agent framework: Pydantic AI (implemented)

**Needs.** One agent that decides its next step itself (no fixed pipeline)
with optional, versioned skills that add guidance and tools;
tool inputs that are validated and cannot carry extra fields such as
identity or approval; a final output that is either an answer with cited
evidence or a clarification question; context rebuilt by the application
before every model request; a tool set that can change between steps; two
model vendors with fallback; and the option of running the same loop
durably.

**Options.**

- **Pydantic AI.** Typed tools and outputs from Pydantic models; an
  [output type](https://pydantic.dev/docs/ai/core-concepts/output/) that can
  be a choice of models; [toolsets](https://pydantic.dev/docs/ai/tools-toolsets/toolsets/)
  that are rebuilt before each step; a
  [`FallbackModel`](https://pydantic.dev/docs/ai/models/overview/); and a
  [Temporal integration](https://pydantic.dev/docs/ai/integrations/durable_execution/temporal/)
  that runs each model request and tool call as an activity while the loop
  stays in the workflow.
- **LangGraph.** A graph of nodes and edges. Graphs can loop and branch
  conditionally ([graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)),
  so a single adaptive tool-calling loop is as natural there as a fixed
  pipeline. It brings [checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers),
  including PostgreSQL, and [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
  for human input. Temporal publishes a
  [LangGraph integration](https://docs.temporal.io/develop/python/integrations/langgraph),
  currently in public preview.
- **A custom loop.** No framework dependency and full control, at the cost
  of owning each provider's tool-call, streaming and structured-output
  protocol, plus output validation and the durable-execution glue.

**Choice.** Pydantic AI. Its typed tool and output contracts match the
project's validation rules directly, its toolset and model abstractions let
the application wrap every model request without forking the loop, and its
Temporal integration made the durable mode a configuration of the same
agent rather than a second implementation. LangGraph would also work: the
difference is in what has to be built around it. Its checkpointing would
either overlap with Temporal's history or replace Temporal with a different
recovery model, and LangGraph's own interrupt-and-resume restarts the
interrupted node, so the same idempotency work is needed either way. A
custom loop was rejected because it would mean owning two vendors' protocols
for little gain.

**What the application owns and what the framework provides.**

| Concern | Owner |
| --- | --- |
| The loop: message history, tool-call dispatch, validation of tool inputs and of the final output | Pydantic AI |
| Fallback order between providers | Pydantic AI `FallbackModel`, wrapped by the application |
| Retries, backoff, primary cooldown, timeouts, per-attempt budget and cost charges | Application (provider SDK retries are off) |
| Context for each request (schema, preferences, evidence, history, persona, budget) | Application (`GuardedModel` asks the runtime before each request) |
| Which tools are exposed, and authorization at execution | Application (skills, current permissions and the capability registry) |
| Skills: catalog, loading, version pinning | Application (bundled assets, recorded per run) |
| Stop decisions, clarification, steering, cancellation, active deadline, model-spend limit | Application lifecycle policy and run budgets, shared by both backends |
| Turning model requests and tool calls into Temporal activities | Pydantic AI `TemporalDurability` |
| Release of answers | Application output privacy gate |

**Costs.** The Gemini Interactions API is not supported by Pydantic AI's
Google model ([open issue](https://github.com/pydantic/pydantic-ai/issues/6192)),
so the project maintains its own model adapter for it. The framework moves
quickly: its older Temporal wrapper is marked for removal in favour of the
capability the project uses. Application and domain code do not import
Pydantic AI; the boundary checks in `tests/architecture` enforce that.

## Framework choice and author experience

I chose **Pydantic AI** because it fits the project's Python architecture and
supports typed tools, validated inputs and structured responses. It lets one
agent choose its next step in a flexible loop, while application code handles
authorization, query validation, budgets and confirmation. This also makes new
capabilities straightforward to add through tools and skills.

My main experience is with **LangGraph**. Pydantic AI is a more recent addition
to my toolkit; this project gave me hands-on experience integrating it with
guarded tools, model fallback, tracing and evaluations.

I also implemented **Temporal as an optional execution backend** to explore
durable workflow recovery. The local demo uses a simpler in-process manager.
At the expected production workload, I would keep Temporal optional and adopt
it when interruption costs or longer investigations justify it.

## Execution: simpler manager first, Temporal optional (implemented)

**Needs.** Investigations continue when the CLI disconnects, wait for a
clarification without calling the model, can be cancelled, and never
repeat a BigQuery job or a deletion. At the expected workload (about 1,000
short questions a day, few at once) losing an in-flight run to a deploy or
crash is rare and cheap to redo.

**Options.** (1) A manager inside one process over PostgreSQL, with no
replay. (2) Temporal workflows on separate workers. (3) A home-grown job
table with leases and replay.

**Choice.** Start with option 1, locally and in production: it meets the
needs with no extra service. An interrupted run is reported honestly and
replaced by a new request; recorded job IDs, reconciliation and idempotent
writes keep a replacement from duplicating external effects. Option 2 is
implemented and tested as an optional backend, adopted when measured
interruption costs, longer investigations or deployment burden justify its
cost ([when to adopt Temporal](production-deployment.md#when-to-adopt-temporal)).
Option 3 was rejected: it means writing a recovery engine.

**Costs and limits.** The simpler manager does not resume a run at its
previous step, and it is one process per database: it runs several
investigations at once but does not scale horizontally. Temporal adds a
service, deterministic workflow code and
[versioning](https://docs.temporal.io/worker-versioning) discipline, and it
does not make external effects happen exactly once: an
[activity may run more than once](https://docs.temporal.io/activity-definition),
so idempotency stays in the application either way. Details:
[investigation runtime](../investigation-runtime.md).

## Models (implemented)

**Needs.** Reliable tool calling and structured output over many steps,
streaming, acceptable latency and cost per question, a different-vendor
fallback, and terms suitable for the data sent.

| Role | Default | API | Why |
| --- | --- | --- | --- |
| Primary agent model | `gemini-3.8-flash` | Gemini Interactions API | Listed as stable on Google's [models page](https://ai.google.dev/gemini-api/docs/models). Measured in this project (below). |
| Fallback agent model | `gpt-5-mini` | OpenAI Responses API | Different vendor, so one provider outage does not stop analysis; supports function calling, structured output and streaming ([model page](https://developers.openai.com/api/docs/models/gpt-5-mini)). Enabled only when a key is set. |
| Embeddings (Golden retrieval) | `gemini-embedding-2` at 768 dimensions in live mode; an offline hashing embedder in fixture mode | Gemini embeddings | 768 is one of the recommended reduced sizes of a model with up to 3072 dimensions ([embeddings](https://ai.google.dev/gemini-api/docs/embeddings)). Retrieval thresholds were tuned for this model. |

Both chat models are configuration, not code. The application owns routing,
retries and budgets, so the model cannot choose a provider or loosen a
policy ([model providers](../model-providers.md)).

**Evidence.**

- A [real-model evaluation](../../evaluation/real-model/README.md) of ten
  conversations: all answered by Gemini with no fallback; 41/41 expected
  figures present in the released evidence and 40/41 stated in the answers.
  Human review of the reports is pending, and no model judge ran.
- A [conversation-efficiency comparison](../../evaluation/real-model/efficiency/results/candidate.md)
  of the same model before and after the context and tool-exposure changes:
  for example an ordinary scalar question went from 9 model requests and
  about 55k tokens to 3 requests and about 16k tokens. At that point the
  model's first query was often rejected by the SQL compiler (an unsupported
  join); after the join guidance was clarified, the first query was accepted
  in 3 of 3 repetitions ([real-model evaluation](../../evaluation/real-model/README.md#restricted-join-guidance-check)).
- Estimated model spend for two simple live questions was USD 0.011 and
  0.013, against a soft limit of USD 1 per question.
- Earlier "live Gemini" runs, before a schema fix, were in fact answered by
  the GPT fallback; only the evaluations above are verified Gemini results.

**Limits.** No other model was evaluated as the primary, and GPT-5 mini has
not been measured as a primary on the same suite, so there is no
like-for-like comparison and no claim that the chosen model is the best
available. Fallback is tested with fault injection and one live test that is
skipped when the free quota is spent. Rate limits depend on the project's
[usage tier](https://ai.google.dev/gemini-api/docs/rate-limits). The local
setup runs on a free-tier Gemini key, whose terms allow Google to use
submitted content; production needs at least a paid tier
([models and data handling](production-deployment.md#models-and-data-handling)).

## Cloud platform (recommended; services proposed)

**Needs.** The analytical data is in BigQuery, and queries run where the
data is. Production also needs a managed PostgreSQL, object storage,
secrets, identity integration, telemetry, an HTTP/SSE API host and
long-running workers.

**Reasoning.** Google Cloud is recommended because the data source is
BigQuery: keeping the application, database and storage in the same project
and region keeps data movement, IAM and networking in one place. That the
primary model is Gemini is not, by itself, an argument: the Gemini Developer
API used by the prototype is not covered by the project's IAM or regional
guarantees, and the OpenAI fallback and Temporal Cloud are separate vendors
in any case. Kubernetes is not needed at this workload; the proposed
services are Cloud Run for the API, Cloud SQL, Cloud Storage and Secret
Manager, with Cloud Run worker pools for Temporal workers if Temporal is
adopted. Region, model hosting, identity and the final compute choices are
open ([production deployment](production-deployment.md#open-decisions)).
Nothing is provisioned.

## Data and storage (implemented)

| Choice | Need | Alternatives | Trade-off |
| --- | --- | --- | --- |
| **PostgreSQL** for all application state | Transactions and row locks for deletion confirmation, budgets, report versions; immutable evidence; ordered event replay | SQLite (weaker concurrency), a document store (no multi-row transactions of the kind used) | One engine locally and in production; bounded JSONB for evidence |
| **Files behind a `BlobStore` port** for report bodies | Immutable, checksummed Markdown that can grow beyond a row | Database column | Two stores to keep consistent; a reconcile command reports drift |
| **In-process Golden retrieval** (BM25 + exact cosine, vectors in PostgreSQL) | Tens of examples, authorization filter before scoring | pgvector, a vector database | Exact search is enough at this size; pgvector is the path when the corpus grows ([benchmark](../../evaluation/retrieval/README.md)) |

## SQL compiler: SQLGlot (implemented)

**Need.** The model writes analytical SQL, but must never choose tables,
products or identifiers it is not allowed to see. **Options.** Regex or
string checks (cannot resolve scopes; unsafe), a view per executive (no
column-level control over derived identifiers, many objects), a parsed and
rewritten syntax tree. **Choice.** SQLGlot parses the BigQuery dialect into a
tree, so the compiler can allowlist node types, resolve CTE and subquery
scopes, and replace each logical relation with a trusted, product-filtered
projection. **Cost.** The grammar is deliberately restricted (no window
functions, set operations or `UNNEST`); some analyses take several simpler
queries, and the model's first attempt is often rejected and reformulated
(see the evidence above). Tests: about 200 adversarial queries plus
property tests ([components](../components.md#restricted-sql-compiler)).

## Interfaces: HTTP + SSE API and a CLI (implemented)

**Need.** A terminal client that survives disconnects and shows progress.
**Choice.** FastAPI with Uvicorn for typed requests and streaming
responses; server-sent events with replay from PostgreSQL; a Click CLI over
httpx that holds no backend credentials. **Alternative.** WebSockets
(bidirectional, but replay and proxies are more work; the client only needs
server-to-client events). A web UI would use the same API.

## Observability (implemented locally)

**Need.** One trace per investigation with the sanitized conversation,
metrics for runs, budgets, estimated model spend, fallbacks and privacy withholds, and no personal
data in telemetry. **Choice.** OpenTelemetry through one sanitizing facade,
exported to MLflow (agent traces with a conversation view), Prometheus and
Grafana, all local with no account. Budget and spend enforcement read
PostgreSQL, never telemetry. **Alternatives.** Hosted tracing services
(accounts and data agreements). In production the company's Grafana can be
reused if one exists, and Google Cloud accepts the same OTLP data
([production deployment](production-deployment.md#parts-and-proposed-services)).
**Limit.** Traces hold masked but still sensitive analytical content, so
access must be restricted ([observability](../observability.md)).

## Supporting libraries (implemented)

- **Alembic with SQLAlchemy Core and psycopg 3**: reviewable, linear
  migrations; domain records stay independent of the ORM.
- **python-dotenv with Pydantic settings**: one explicit loader whose
  validation errors never include secret values.
- **PyJWT**: explicit algorithm allow-lists and required claims; the same
  library verifies identity-provider keys (JWKS) later.
- **google-cloud-bigquery**: dry runs, `maximum_bytes_billed` and
  client-chosen job IDs, which make lost submissions recoverable.
- **Ruff, mypy (strict) and pytest** for checks, with architecture tests for
  layer boundaries.
