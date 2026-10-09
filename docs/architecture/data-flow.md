# Data flow and trust boundaries

This page shows how a request moves through the system, where each kind of
data is stored, and which component is trusted with what. Unless a line says
"proposed", it describes the implemented local prototype.

## Investigation: question to grounded answer

```mermaid
sequenceDiagram
    autonumber
    actor U as Executive
    participant C as CLI
    participant A as API
    participant R as Runner (local manager or Temporal worker)
    participant M as Model (Gemini, GPT backup)
    participant G as Guards (compiler, result boundary, output gate)
    participant B as BigQuery
    participant P as PostgreSQL

    U->>C: question
    C->>A: POST /v1/sessions/{id}/runs (bearer token, submission_key)
    A->>A: verify token, resolve executive
    A->>P: store message and run (one active run per session)
    A->>R: schedule run
    A-->>C: run id
    C->>A: GET /v1/runs/{id}/events (SSE)
    loop each agent step, within the run budget
        R->>P: reserve provider request in the run budget
        R->>P: rebuild context under current authority
        R->>M: instructions, context, tool catalog
        M-->>R: tool call (for example execute_analysis)
        R->>G: tool call with trusted execution context
        G->>P: re-resolve entitlements, record operation and job id
        G->>B: dry run, then the compiled scoped query (byte cap, deadline)
        B-->>G: rows
        G->>G: result boundary (shape, lineage, references, bounds)
        G->>P: immutable evidence record
        G-->>R: compact result with evidence id
        R->>P: progress event
        P-->>A: events, polled in order
        A-->>C: SSE progress
    end
    M-->>R: answer draft citing evidence ids
    R->>G: output privacy gate (fresh authority)
    G-->>R: release all sections or none
    R->>P: answer and run outcome
    A-->>C: SSE final answer
```

Notes:

- The agent can stop for a clarification at any step. The question is stored,
  the active-time clock pauses, and no model call is made until the user
  answers through `POST /v1/runs/{id}/answers`.
- With Temporal, the model call and each tool call are activities. Their
  inputs and results are evidence references and drafts, not result rows.
  Context is rebuilt inside the activity. With the local manager, the same
  steps run as asyncio tasks in the API process.
- A disconnect does not cancel the run. `Last-Event-ID` replays the events
  from PostgreSQL.

## Destructive operation: report deletion

```mermaid
sequenceDiagram
    autonumber
    actor U as Executive
    participant C as CLI
    participant A as API
    participant M as Model
    participant D as Deletion service
    participant P as PostgreSQL

    U->>C: "delete the reports we made in this conversation"
    C->>A: message
    M->>D: search_reports / list_reports (owner only)
    M->>D: propose_report_deletion(exact report ids)
    D->>P: proposal frozen at current versions, expires in 10 minutes
    D-->>C: deletion.proposed event (proposal id only)
    C->>A: GET /v1/deletion-proposals/{id}
    A-->>C: preview: titles, dates, count, expiry
    Note over M,D: The model has no confirm tool, and its arguments cannot carry approval.
    U->>C: confirm <proposal-id>
    C->>A: POST /v1/deletion-proposals/{id}/confirm {"confirm": true}
    A->>D: confirm(principal, proposal id)
    D->>P: one transaction: lock, recheck owner, expiry and versions,<br/>soft delete, withdraw reuse links, consume proposal, audit
    P-->>D: committed, or no change at all
    D-->>C: deleted count, recoverable for 7 days
```

Reports created after the proposal are never included. If any proposed report
changed, the whole proposal is stale and nothing is deleted.

## Storage map

| Data | Where (local) | Where (production, proposed) | Retention |
| --- | --- | --- | --- |
| Executives, roles, product entitlements, authorization version | PostgreSQL | Cloud SQL | Until changed; every change is audited |
| Sessions, messages, runs, ordered progress events, clarification inputs | PostgreSQL | Cloud SQL | 7 days after the later of last interaction and run completion |
| Operations and BigQuery job references | PostgreSQL | Cloud SQL | With the investigation. Unresolved operations are kept and flagged for manual resolution |
| Evidence (bounded released rows and provenance, JSONB) | PostgreSQL | Cloud SQL | With the investigation, or while a saved report pins it |
| Run budgets and charges | PostgreSQL | Cloud SQL | With the investigation |
| Saved report metadata, versions, evidence links, required product scope | PostgreSQL | Cloud SQL | Until deleted; soft-deleted reports are restorable for 7 days, then purged |
| Report bodies (Markdown) | Artifact directory or volume | Cloud Storage | Same as the report |
| Deletion proposals | PostgreSQL | Cloud SQL | Expire after 10 minutes |
| Preferences (explicit, confirmed inferred) | PostgreSQL | Cloud SQL | Until changed or forgotten |
| Golden examples, review history, provenance, embeddings | PostgreSQL | Cloud SQL (pgvector when needed) | Independent lifecycle: retire, suspend, erase |
| Persona versions, publications, run pins | PostgreSQL | Cloud SQL | Version history kept |
| Audit events (identifiers, versions, counts; never content) | PostgreSQL | Cloud SQL | 90 days by default (configurable) |
| Temporal workflow history (opt-in locally) | Separate PostgreSQL database | Temporal Cloud | 7 days after a workflow closes |
| Traces (sanitized) | MLflow | MLflow (Cloud SQL + Cloud Storage) | Backend defaults; no project policy yet |
| Metrics | Prometheus | Managed Service for Prometheus | Backend defaults |
| Retail transactions | BigQuery public dataset (read-only) | Company-owned BigQuery dataset | Not managed by this application |

Cleanup runs as a bounded, idempotent maintenance command
(`retail-analytics-maintenance cleanup`), started by an external scheduler. It
deletes database rows and artifact files only. Backups, snapshots and
write-ahead logs need their own retention policy before full physical erasure
can be claimed.

## Trust boundaries

```mermaid
flowchart TB
    subgraph untrusted["Untrusted input"]
        user["User text"]
        modelout["Model output: tool arguments, SQL, drafts"]
        retrieved["Stored text: report titles, examples, persona drafts"]
    end

    subgraph trusted["Trusted application code"]
        auth["Token verification and authority resolution"]
        registry["Capability registry<br/>(no identity, budget or approval arguments)"]
        compiler["SQL compiler<br/>(allowlisted grammar, trusted binding, product scope)"]
        boundary["Result privacy boundary"]
        gate["Output privacy gate"]
        confirm["Confirmation handler<br/>(explicit user action only)"]
        budgets["Run budgets"]
    end

    subgraph data["Data and services"]
        bq[("BigQuery")]
        pg[("PostgreSQL")]
        providers["Model providers"]
    end

    user --> auth
    modelout --> registry
    registry --> budgets
    registry --> compiler --> bq
    bq --> boundary --> pg
    retrieved --> gate
    modelout --> gate
    gate --> pg
    confirm --> pg
    auth --> registry
    pg -- "masked, authorized context" --> providers
```

What each boundary enforces:

| Boundary | Rule | Status |
| --- | --- | --- |
| Identity | A bearer token is verified on every request. Identity never comes from a request body, and tools never receive identity or entitlements as arguments. | Implemented (local HS256; production IdP proposed) |
| Authority | Entitlements and permissions are reloaded on every attempt, including inside retried activities. An empty product scope means no data. | Implemented |
| SQL | The model writes SQL over logical relations only. The compiler rejects anything it cannot resolve, and adds the product filter at every physical read before aggregation. | Implemented |
| Privacy | Direct identifiers never reach the model. Customers, orders and items appear only as per-executive opaque references, and ages only as bands. Results are checked again before release. | Implemented |
| Output | Each answer, report, memory entry and progress text passes the output gate. Citations must be usable now. | Implemented |
| Destructive actions | The model can propose a deletion. Only an authenticated user action can confirm it. | Implemented |
| Model providers | Only masked, authorized context leaves the backend. Requests send `store: false`. | Implemented. The free tier of the Gemini API may use submitted content to improve Google products, so production needs a paid tier or an equivalent agreement (see [production deployment](production-deployment.md)) |
| Telemetry | Attributes are allowlisted and redacted. Prompts, SQL and rows are never exported. | Implemented |

The privacy policy is pseudonymization, not anonymization. Demographic
combinations (country, state, age band) are allowed and can still single
people out. Detectors for names in free text are defense in depth, not proof;
see [known limitations](known-limitations.md).
