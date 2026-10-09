# Extension contracts

New capabilities, such as charts, e-mailed reports or web trend search, are
added as **capabilities** behind the existing tool gateway. The agent loop,
the runtimes and the security checks do not change. This page describes the
contracts that exist today and how the three example extensions would use
them.

## Implemented contracts

### Capability registry

A capability is one `CapabilitySpec` registered at the composition root
(`application/tools/registry.py`):

| Field | Purpose |
| --- | --- |
| `name`, `version`, `description`, `progress_label` | What the model sees, and the application-authored text of the progress event |
| `input_model`, `output_model` | Pydantic models with `extra="forbid"` |
| `handler` | Receives the validated input and a trusted `OperationContext` (identity, product scope, budgets, operation ID) |
| `authorization` | Required permissions, and whether an empty product scope hides the tool. Checked again on every execution. |
| `side_effect` | `read_only`, `idempotent_write`, `external_job` or `external_delivery` |
| `retry` | `retry`, `reconcile_first` or `no_retry` |

When a spec is constructed, the registry refuses:

- any input field whose name suggests identity, entitlements, permissions,
  budgets, confirmation, approval or credentials, including nested fields;
- permissive input models;
- blind retries of external effects. An `external_job` or
  `external_delivery` capability must either reconcile before retrying or
  not retry at all.

The catalog shown to the model depends only on the trusted context, never
on an investigation stage. Every call creates a durable operation record
with a stable ID that serves as the idempotency key. Telemetry,
budgets and the output privacy gate apply to all capabilities.

**Implemented examples.** Discovery, guarded analysis, evidence fetch, Golden
retrieval, preferences, reports, deletion proposals and `convert_currency`.
Currency conversion was added after the core loop, as an extension: a new
external port (exchange rates), derived evidence that keeps its source, and a
disclosure for the declared source currency.

### Artifacts

`ArtifactService` stores immutable, checksummed, owner-scoped files through
the `BlobStore` port (local filesystem now; Cloud Storage proposed). Markdown
reports use it today. PNG, JPEG and PDF media types are reserved, with a
10 MiB binary limit.

### Evidence

Every result that supports a claim is immutable evidence with provenance.
Derived evidence names its inputs. The output gate only accepts citations to
evidence the caller may use now.

## Example extensions (proposed, not built)

| Extension | Shape | Contract details |
| --- | --- | --- |
| **Charts** | Capability `render_chart`, `idempotent_write` | The input names evidence IDs and a chart spec (type, fields), never data values. The handler reads authorized evidence, renders a PNG or SVG with a plotting library in the backend, and saves it as an artifact. The chart is derived evidence, so the gate and citations apply. The CLI shows a link or exports the file. A "prefers charts" preference kind would be added then. |
| **E-mail a report** | Capability `propose_report_email`, plus an application confirm endpoint, `external_delivery`, `no_retry` | Like deletion: the model can only propose a send of an exact report version to recipients from a company directory allowlist. The user confirms. The application sends it through an e-mail port (for example a transactional mail API) with the operation ID as the idempotency key. An uncertain send is recorded as unresolved, never re-sent blindly. The report passes the output gate again, under a new e-mail destination. |
| **Web trend search** | Capability `search_web_trends`, `read_only`, `retry` | Through a search-API port. The query text is screened so no customer data, references or restricted figures leave the backend. Results are untrusted text: they are quoted, stored as external evidence with source URLs, and labelled as external context, not company data. Budgets get a separate counter for search calls. |

Each extension also needs fixture tests through the gateway, adversarial
cases for its new input surface, and a telemetry span and metric labels from
the fixed vocabulary.

## Other extension points

| Point | Port | Implemented adapters |
| --- | --- | --- |
| Identity provider | `TokenVerifier` | Local HS256 (an OIDC/JWKS verifier is proposed) |
| Execution backend | `InvestigationScheduler` | Local manager, Temporal |
| Model provider | Pydantic AI `Model` + application chain | Gemini Interactions, OpenAI Responses |
| Embeddings | `TextEmbedder`, `EmbeddingStore` | Hashing (offline), Gemini; PostgreSQL store |
| Warehouse | `WarehouseQueryJobs` | BigQuery |
| Exchange rates | rate provider port | Frankfurter (ECB), fixture |
| Telemetry | telemetry port | OpenTelemetry (MLflow, Prometheus) |
| Web client | HTTP + SSE API | CLI only; a web UI would use the same API and event stream |
