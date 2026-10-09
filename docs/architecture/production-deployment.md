# Production deployment

**Status: designed, not provisioned.** Nothing on this page is deployed or
tested in a cloud. The page separates three kinds of statement:

- **Agreed direction**: decisions about how production should work, which
  the documentation and the code already follow.
- **Proposed**: a specific cloud service suggested for a part, with its
  alternative. Proposed services are not selected or purchased.
- **Open**: choices that still need information (listed at the end).

Vendor facts link to official documentation read on 2026-10-09; recheck them
before any provisioning. The application depends on narrow ports (database,
blob store, token verifier, warehouse jobs, model chain, execution
scheduler), so changing a service replaces an adapter or infrastructure,
not application rules.

## Requirements and measurements

**Workload (design input).** One retail company with tens to a few hundred
executives: about 100 active managers a day, about 1,000 questions and 100
saved reports a day, only a few investigations at the same time, and about
one million new warehouse rows a day. Targets: 10 to 30 seconds for a simple
answer, one to two minutes for a report. No peak-load figure, availability
target or recovery objective has been set.

**Access.** Managers may analyze only their assigned brands and the orders
and customers related to those brands' items; the CEO sees every brand
through an explicit grant. Customer demographics are available only as
group-level aggregates. Both rules are implemented in the prototype
([brand-based access](../brand-access.md), [privacy](data-flow.md#trust-boundaries)).

**Cost.** About USD 1 per question is treated as configurable *estimated
model spend*, summed over every model request of the question (retries and
fallback included). BigQuery, storage and infrastructure costs are separate.
This is a design assumption about what the target covers.

**Measured so far** (local backend, single user, one model):

| Measure | Result | Source |
| --- | --- | --- |
| Simple revenue question against live BigQuery | 21 to 27 s end to end, 2 model requests, 1 query | [live smoke](../../evaluation/real-model/efficiency/results/live-smoke.json) |
| Estimated model spend per simple question (live) | USD 0.011 and 0.013 for two questions | [real-model evaluation](../../evaluation/real-model/README.md#live-smoke-http-api-and-real-bigquery) |
| Offline suite: simple questions / report investigation | median 12 to 23 s / about 70 s of active time | [efficiency results](../../evaluation/real-model/efficiency/results/candidate.md) |

These are small samples, not percentiles under load.

## Agreed direction

1. **Start with the simpler execution backend.** At this workload,
   investigations are short and cheap and few run at once, so production
   starts without durable replay, as the local default does. If the process
   running an investigation stops (deploy or crash), the run is marked
   *interrupted* and the user sends the request again; it is not resumed at
   its previous step. This is not free of consequences: an interrupted run
   may already have submitted a billed BigQuery job or completed a report
   save. Recorded job and operation IDs, reconciliation of uncertain work and
   idempotent writes stay in place, and destructive actions or stale
   confirmations are never replayed automatically. A replacement run may
   repeat analytical work, never a confirmed external operation.
2. **Temporal is a supported optional backend**, adopted when measurements
   show it pays off (see [when to adopt Temporal](#when-to-adopt-temporal)).
   The Temporal mode is implemented and tested locally; choosing it changes
   hosting, not application rules.
3. **One adaptive agent.** The same loop, skills, guarded tools and stores
   run under either backend ([the agent loop](agent-loop.md)).
4. **Bounded work per question.** A hard 120-second active-work deadline
   (clarification waits excluded) and a soft limit on estimated model spend
   are implemented and enforced from PostgreSQL, independent of telemetry.
5. **Progress now, answer streaming later.** The user sees truthful progress
   written by the application while the run works; the answer is released
   only after the whole answer passes the output checks. Releasing validated
   answer sections incrementally is a planned extension
   ([below](#planned-validated-answer-streaming)).
6. **Kubernetes is not required** for this workload.
7. **Storage**: PostgreSQL for application state, object storage for report
   files, Golden retrieval kept fast by indexing in PostgreSQL rather than
   reading objects per question.
8. **Monitoring** reuses the company's Grafana if one exists (it still needs
   a metrics backend and an owner); model and tool traces (MLflow today)
   and the transactional audit stay separate.

## Topology

```mermaid
flowchart TB
    cli["analytics CLI (executive laptop)"]
    idp["Company identity provider (open)"]
    cli -- "sign-in" --> idp

    subgraph gcp["Google Cloud project, one region (services proposed)"]
        api["API service, HTTP + SSE<br/>Cloud Run"]
        subgraph exec["Shared execution boundary: same agent loop, skills, guarded tools, budgets"]
            simple["(a) Initial: simpler manager<br/>one long-lived process (today the<br/>API process), several investigations"]
            workers["(b) Optional: Temporal workers<br/>Cloud Run worker pool,<br/>fixed small capacity"]
        end
        subgraph stores["Stores"]
            sql[("Cloud SQL for PostgreSQL<br/>records, evidence, budgets and spend,<br/>report and Golden metadata, embeddings, audit")]
            gcs[("Cloud Storage<br/>report files, Golden originals")]
            bq[("BigQuery<br/>company-owned dataset")]
        end
        obs["Grafana + metrics backend,<br/>MLflow traces"]
    end

    tc[["Temporal Cloud (optional)<br/>workflow progress, timers, task queue"]]
    llm["Model providers (external)<br/>Gemini; OpenAI fallback if allowed"]

    cli -- "HTTPS + SSE, bearer token" --> api
    api -- "schedule run" --> simple
    api -. "optional: start workflow, signal" .-> tc
    tc -. "task queue (workers poll)" .-> workers
    api -- "sessions, events, reports" --> sql
    exec -- "read/write; files; scoped queries" --> stores
    exec -- "masked context" --> llm
    exec -. "OTLP" .-> obs
```

Legend: solid arrows are the initial path; dotted arrows are optional or
best effort. Every workload reads its secrets from Secret Manager with its
own service identity (not drawn). Paths (a) and (b) are **alternative deployments**, not two
engines executing the same run and not a fallback switched during a run.
Temporal persists workflow progress and hands work to the workers; it does
not store application records or call models itself. PostgreSQL stays the
authority for business data either way. The API never queries BigQuery or
calls models.

**Hosting constraint of path (a).** The simpler manager guarantees one
manager per database with a PostgreSQL lock held by its process. It runs
several investigations concurrently (bounded by `LOCAL_MAX_CONCURRENT_RUNS`)
but is **not** horizontally scalable: production would run exactly one such
process, for example one long-lived instance (Cloud Run with a single
instance, or a small Compute Engine VM). Platform restarts of that instance
interrupt running investigations, which is the accepted trade-off of path
(a). Today the manager runs inside the API process, so with path (a) the
API service also runs as a single instance; running the manager as a
separate process is a deployment change that is not built. Path (b) has no
such limit: API instances and workers scale independently.

## Parts and proposed services

| Part | Proposed | Alternative | Notes |
| --- | --- | --- | --- |
| Cloud platform | Google Cloud | Another cloud with BigQuery access | The data is in BigQuery, and queries run in the data's location; keeping the application, database and storage in the same project and region simplifies IAM and networking. The primary model being Gemini is not a reason by itself (see [models](#models-and-data-handling)). |
| API + SSE | [Cloud Run service](https://docs.cloud.google.com/run/docs/triggering/https-request) | Compute Engine | HTTP streaming needs no setup. The [request timeout](https://docs.cloud.google.com/run/docs/configuring/request-timeout) defaults to 5 minutes and can be raised to 60; at the limit the stream closes and the CLI reconnects and replays from PostgreSQL (implemented). |
| Initial execution (a) | One long-lived process running the API and the simpler manager | — | See the hosting constraint above. Open: Cloud Run with a single instance or a small VM. |
| Optional execution (b) | [Temporal Cloud](https://docs.temporal.io/cloud/namespaces) with workers on [Cloud Run worker pools](https://docs.cloud.google.com/run/docs/deploy-worker-pools) | Workers on a small Compute Engine deployment; self-hosted Temporal ([MIT, PostgreSQL/MySQL/Cassandra persistence](https://docs.temporal.io/temporal-service/persistence)) | Worker pools are for continuous background work and are [scaled manually](https://docs.cloud.google.com/run/docs/configuring/workerpools/manual-scaling): start with a small fixed count with bounded concurrency and plan capacity from queue delay; no built-in queue autoscaling is assumed. Temporal Cloud is Temporal's managed service (also sold through Google Cloud Marketplace), not a Google-run server. |
| Application database | [Cloud SQL for PostgreSQL](https://docs.cloud.google.com/sql/docs/postgres/high-availability) | A zonal instance with backups | Regional HA keeps a synchronous standby in a second zone, failover in about a minute; [point-in-time recovery](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/pitr); [IAM database authentication](https://docs.cloud.google.com/sql/docs/postgres/iam-authentication) through a connector. Whether HA is needed depends on recovery objectives (open). |
| Report files and Golden originals | [Cloud Storage](https://docs.cloud.google.com/storage/docs/storage-classes) behind the `BlobStore` port (adapter not built) | Database column for small Markdown | Retrieval reads the indexed representation in PostgreSQL, not objects per question ([below](#golden-retrieval-at-scale)). Object versioning helps against mistakes but purge must then remove noncurrent versions. |
| Warehouse | BigQuery, company-owned dataset | — | The SQL compiler stays the enforcement boundary; [row-level security](https://docs.cloud.google.com/bigquery/docs/row-level-security-intro) can add a second filter on an owned table. [Cost controls](https://docs.cloud.google.com/bigquery/docs/best-practices-costs): `maximum_bytes_billed` per job (implemented) plus project quotas. |
| Identity | Company OIDC provider with a JWKS verifier behind `TokenVerifier` (not built); CLI device authorization grant ([RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)) | — | Provider open. Roles and brand assignments stay server-side. |
| Secrets | [Secret Manager](https://docs.cloud.google.com/secret-manager/docs/overview), read with each workload's own service identity | — | No secret in images or environment files. |
| Metrics and traces | The company's Grafana with a metrics backend (for example [Cloud Monitoring through OTLP](https://docs.cloud.google.com/stackdriver/docs/otlp-metrics/overview), queryable with PromQL), MLflow for agent traces | Self-hosted Prometheus and Grafana | Owner open. Model spend appears in [MLflow](https://mlflow.org/docs/latest/genai/tracing/token-usage-cost/) and Grafana, but enforcement never depends on them. |

## Models and data handling

The prototype's primary model is Gemini (`gemini-3.8-flash`) with GPT-5 mini
as a different-vendor fallback. Gemini was chosen as the preferred provider
and performed well on this project's evaluations; no like-for-like
comparison with GPT or Claude models has been run, so there is no claim that
it is the best choice.

- **Gemini Developer API** (API key, used by the prototype). Under the
  [Gemini API terms](https://ai.google.dev/gemini-api/terms), unpaid
  services may use submitted content to improve Google products; paid
  services do not, but data may be processed or cached in any country where
  Google has facilities. Production needs at least the paid tier.
- **Gemini on Vertex AI** offers service-account IAM, VPC Service Controls
  and regional endpoints with
  [data residency](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/data-residency).
  The Interactions API the prototype uses is a preview there, so moving
  would need adapter work and a new evaluation.
- **OpenAI** (fallback). [API data](https://developers.openai.com/api/docs/guides/your-data)
  is not used for training by default and abuse-monitoring logs are kept up
  to 30 days. Whether masked analytical prompts may go to a second vendor in
  production is open.

**Before selecting production models (planned, not run):** compare specific
Gemini, GPT and Claude candidates on the same frozen scenarios, tools,
policies and scoring: numeric correctness, SQL acceptance, grounded
explanations, privacy and access behaviour, unnecessary calls, latency and
full cost per question. A proposed production refinement is model tiers: a
default model for straightforward work and an evaluated stronger model for
difficult investigations, with escalation kept separate from availability
fallback, its reason recorded and every call charged to the same question.
This is a proposal to evaluate, not prototype behaviour.

## Model spend

**Implemented (local and shared runtime).** After each model request
settles, its estimated cost is computed from the provider-reported tokens
(input, cached input, output and reasoning counts, never reasoning text) and
public prices, and charged durably to the run. When the run's estimated
spend reaches the limit (`RUN_MAX_MODEL_COST_USD`, default 1), no further
model request starts; the run ends with its verified findings. It is a soft
limit: the request in flight can overshoot. Unknown usage is never counted
as zero, and a model without a known price is refused while a limit applies.
Details: [run budgets](../components.md#run-budgets-and-recovery).

**Production extension (proposed).** Reserve a conservative maximum cost
atomically before each request (including concurrent requests), reconcile
with actual usage afterwards, keep reservations for uncertain submissions,
version and override prices, and reconcile periodically with provider
billing. Estimated execution cost stays distinct from invoices and shared
infrastructure. Post-call estimates alone cannot give an invoice-exact hard
cap.

**Tuning cycle.** Metrics show spend, latency, stop and partial rates;
traces explain individual expensive or slow runs. Changes to prompts,
skills, evidence reuse, caching, Golden retrieval or model policy are made
in bounded steps and re-evaluated for correctness as well as cost. Budgets
are not raised to hide inefficiency, and quality is not reduced to meet a
spend target. High spend alone is not evidence of a valuable investigation.

## Planned: validated answer streaming

Today the answer is released whole after the output checks; progress is
shown meanwhile. A future extension would release buffered answer sections
from the same model loop as soon as each section passes a deliberately
designed incremental check (privacy, current access, citations, numbers and
completeness), keeping an unreleased tail for content that spans
boundaries and buffering whole any answer whose rules need full context.
Access would be rechecked before each release; displayed text cannot be
recalled, and the design must say so. Section IDs would make replay free of
duplicates, and a fallback after partial delivery must not append a
contradictory replacement. A rollout needs adversarial split-boundary tests
(personal data, references, structured data), scope revocation, incomplete
citations, fallback, reconnect and latency and cost measurements. Revealing
an already validated answer gradually would not be streaming and would not
shorten the time to useful content. **Not implemented.**

## When to adopt Temporal

Temporal provides durable workflow progress, timers, signals and activity
retries, so after a worker failure or a deploy an investigation continues
where it stopped. It does not make external effects exactly once: an
[activity may run more than once](https://docs.temporal.io/activity-definition),
so application idempotency, job-ID reconciliation, transactional report and
audit writes and safe version compatibility stay necessary either way.

Adoption is a cost and value decision, reviewed with measurements rather
than a fixed traffic threshold:

- interruption and failure rates, and how often users abandon after one;
- model and warehouse spend repeated by replacement runs;
- active time and cost distributions, longer multi-step investigations and
  clarification durations;
- the operational burden of deploying without interrupting work;

compared with Temporal's costs. [Temporal Cloud pricing](https://temporal.io/pricing)
(checked 2026-10-09; see also its [pricing documentation](https://docs.temporal.io/cloud/pricing)):
the pay-as-you-go plan has no base fee and starts at USD 50 per million
actions, plus storage and a support fee; the Business plan has a monthly
minimum. Worker hosting is separate. A model call is not one action:
workflows, activities, retries, signals and timers all count, so an estimate
needs measured action counts from representative histories. As an
illustration only, at 30,000 questions a month, 20, 50 or 100 actions per
question would be 0.6, 1.5 or 3 million actions, about USD 30, 75 or 150
before support, storage and hosting. Low measured cost could justify earlier
adoption; more expensive or deeper investigations make lost progress more
costly.

### Temporal topology (optional backend)

```mermaid
flowchart TB
    api["API instances"]
    subgraph ns["Temporal namespace (Temporal Cloud or self-hosted)"]
        tq["Task queue: investigations"]
        hist[("Workflow histories")]
    end
    subgraph pool["Worker deployment (fixed small capacity)"]
        w1["worker"]
        w2["worker"]
        w3["worker ..."]
    end
    pg[("PostgreSQL: business records,<br/>progress events, budgets, operations")]

    api -- "start workflow (id = run id), signal input/cancel" --> ns
    w1 & w2 & w3 -- "poll, heartbeat" --> tq
    w1 & w2 & w3 --> pg
    api --> pg
```

- **Separation of authority.** Temporal is authoritative for scheduling and
  recovery. PostgreSQL is authoritative for business records, the
  progress-event read model and run budgets. Workflow IDs reject duplicate
  starts. Operation IDs stay stable across activity retries. Inputs commit
  to PostgreSQL before the workflow is signalled, and a dispatcher re-sends
  intents that were not delivered. All of this is implemented and tested
  against a local Temporal server.
- **Crash recovery.** If a worker dies, Temporal reschedules the activity on
  another worker. An activity
  [may run more than once](https://docs.temporal.io/activity-definition), so
  Temporal alone does not make a BigQuery job or a report save happen
  exactly once. Effects are made idempotent by the application: BigQuery
  jobs are looked up by their recorded ID before any resubmission, and
  report saves use idempotency keys. The opt-in local Temporal mode runs a
  test that kills a worker after an external effect commits and checks that
  the effect happens once.
- **Payload privacy.** Activities pass evidence IDs and drafts, not result
  rows. Model messages, tool arguments and drafts can still appear in
  workflow history. Production should add a
  [payload codec](https://docs.temporal.io/production-deployment/data-encryption) that encrypts
  payloads before they reach the Temporal service, with keys in the company's key
  management. **Proposed, not implemented.**
- **Deployments.** Activity, workflow and error type names are recorded in
  histories and must stay stable. Workflow code changes roll out with
  [Worker Versioning](https://docs.temporal.io/worker-versioning),
  so running investigations finish on the code that started them. The
  Docker test suite already replays workflow histories to check determinism;
  a release pipeline would also replay histories recorded from the previous
  version (proposed).
- **Scaling.** Concurrency per worker is bounded. Model calls dominate run
  time, so worker count follows provider rate limits and run budgets, not
  CPU.

### Limits of the simpler manager (implemented)

| Limit | Production handling |
| --- | --- |
| Single process, best effort. A run survives CLI disconnects but not the API process. Graceful shutdown marks running investigations as interrupted. After a crash they are marked interrupted at the **next** start. Nothing is replayed or resumed; the user sends a new request. | Temporal resumes the workflow on another worker. |
| The single-manager guarantee is a PostgreSQL advisory lock tied to one database connection. If that connection drops, the lock is released silently and a second manager could start. | Temporal coordinates server-side with task queues, activity timeouts and heartbeats, and supports many workers. A production deployment without Temporal would need real leases or leader election, which is not built. |
| Discarding a queued request and writing its user notice are two separate writes. A failure between them can leave the request discarded without the explanation. It stays in history and is never executed. | Not applicable with Temporal, which promotes queued requests in order. |
| A failing step is not retried locally; the run stops with its verified findings. | Temporal retries activities within the run budget. |

Runs never move between backends. The API refuses to start while the other
backend still has active runs.

## Golden retrieval at scale

**Implemented:** BM25 and exact cosine similarity run in process over a small
corpus. Vectors are stored in PostgreSQL, keyed by content digest, model and
dimensions, so restarts make no embedding calls. Authorization and
applicability filters run *before* scoring. pgvector is not used: the pinned
local PostgreSQL image does not include it, and exact search over tens of
examples is cheap. Live mode embeds with `gemini-embedding-2` at 768
dimensions, the configuration the retrieval thresholds were measured with;
the offline hashing embedder is used in fixture mode.

**Proposed for production:** Golden originals (the versioned question, SQL
and report trios) live in object storage; PostgreSQL holds their approval,
version and permission metadata, searchable text and embeddings, so a
question is served from the index without downloading objects. Index
invalidation keeps honouring withdrawal and authorization. When the corpus
grows to thousands of examples or memory per process becomes a concern:

1. Enable pgvector on Cloud SQL and store vectors in a `vector` column next
   to the existing embedding rows.
2. Keep the eligibility filter (product scope, schema and metric versions,
   review status) in the same SQL query as the similarity search, so
   ineligible examples are never candidates.
3. Start with exact search. pgvector does exact nearest-neighbour search by
   default, with perfect recall. Add an HNSW index only when latency
   requires it. An approximate index can change results, so rerun the
   retrieval benchmark and compare recall against exact search before
   switching. Filtered approximate search can also return fewer rows than
   requested.
4. Keyword search can move to PostgreSQL full-text search. Fusion
   (weighted reciprocal rank) and delivery rechecks stay in application code.

## Security and network (proposed)

- Public ingress for the API only; the database, workers and any
  telemetry UI are private. Telemetry UIs sit behind the company identity
  provider.
- One service identity per workload with least privilege. Only workers get
  BigQuery job access; the API gets none. Workers and the API both get the
  Cloud SQL client role. The maintenance job gets storage delete rights only
  on the artifact bucket.
- A separate database role per component: application, migrations, and
  any self-hosted telemetry store. Model-generated SQL never runs against PostgreSQL.
- BigQuery jobs keep the compiled `maximum_bytes_billed`. Project-level
  custom quotas add a daily ceiling.
- The customer-reference master key is long-lived. Rotating it retires every
  existing customer reference, so rotation is a planned event.

## Backup, retention and failure recovery

| Concern | Proposed handling |
| --- | --- |
| Database loss or corruption | Cloud SQL automated backups and point-in-time recovery; regional HA for zone failure. Recovery objectives (RPO/RTO) are not yet agreed. |
| Artifact loss | Object versioning; bucket in the same region as the database. A restore must keep artifact metadata and blobs consistent. The existing `reconcile` maintenance reports metadata whose file is missing. |
| Temporal history | 7 days for closed workflows (the local setting; Temporal Cloud allows 1 to 90). Business records do not depend on it. |
| Retention jobs | The existing maintenance command runs on a schedule (a scheduled job on the chosen compute). It is idempotent and bounded, so overlapping runs are safe. |
| Erasure | Purge deletes rows and artifact files. Backups, PITR logs and noncurrent object versions keep copies until they expire. Erasure is complete only after the longest backup retention, so that policy must be set and published. |
| Provider outage | Gemini to GPT fallback with a cooldown (implemented). If both providers fail, the run stops with verified findings. |
| BigQuery outage or timeout | Deadline, cancellation, then reconciliation by job ID (implemented). Schema discovery serves the last validated snapshot for up to 24 hours (implemented). |
| Telemetry outage | Exports are dropped and counted; requests are unaffected (implemented). Audit records are in PostgreSQL transactions and do not depend on telemetry. |
| Region failure | Not designed. A single region is assumed; multi-region would need Cloud SQL cross-region replicas and, with Temporal Cloud, a [high-availability namespace](https://docs.temporal.io/cloud/high-availability). |

## Open decisions

1. Region and data residency, which decide between the Gemini Developer API
   and Vertex AI (and the exact API features available there), and whether
   an external fallback provider may receive masked prompts.
2. The identity provider, who administers brand assignments, and how
   assignments are synchronized and revoked ([brand access](../brand-access.md#open-questions-access-lifecycle-out-of-scope)).
3. Backup and recovery objectives (RPO/RTO), which decide database HA.
4. The system owner and the monthly fixed-cost allowance.
5. Final hosting for the initial execution process and, if adopted, the
   Temporal hosting and worker platform.
6. Who operates monitoring (the company's Grafana or a self-hosted stack).

## Not provisioned and not verified

No cloud resource was created for production. There is no infrastructure
code, no load test, no availability target, no alert rules, no penetration
test and no production identity provider. Before calling this deployment
production-ready, it needs infrastructure as code, a staging environment, the
security and recovery tests rerun on it, and agreed service targets.
