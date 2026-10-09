# Investigation runtime

Each investigation runs one Pydantic AI agent that can explore, call tools,
ask a clarification or produce an answer. Analytical guidelines do not impose
a fixed sequence of stages. Two execution backends run the same application
steps and the same agent; they differ only in how work survives a process
ending.

## Selecting the execution backend

`RETAIL_ANALYTICS_EXECUTION_BACKEND` chooses where investigations execute. It
is independent of `RETAIL_ANALYTICS_MODE`: fixture and live (real Gemini/GPT
and BigQuery) both work with either backend.

| | `local` (default) | `temporal` (opt-in) |
| --- | --- | --- |
| Executes in | `retail-analytics-api` (one process) | `retail-analytics-worker` (Temporal workflows); the API only schedules |
| Services | PostgreSQL | PostgreSQL, Temporal server and namespace |
| CLI disconnect | the run keeps going | the run keeps going |
| API/worker process stops or dies | running investigations end **interrupted** (never resumed); the user sends a new request | the workflow resumes on a worker |
| Settings | `RETAIL_ANALYTICS_LOCAL_MAX_CONCURRENT_RUNS` (4), `RETAIL_ANALYTICS_LOCAL_SHUTDOWN_GRACE_SECONDS` (10) | `RETAIL_ANALYTICS_TEMPORAL_ADDRESS` (required), `_TEMPORAL_NAMESPACE`, `_TEMPORAL_TASK_QUEUE` |

`./scripts/bootstrap.sh` and `./scripts/dev.sh` follow the setting; both also
accept `--execution-backend local|temporal` for one run (bootstrap writes it
to the environment file only when the key is new; an explicit value is never
changed). With `local`, dev starts PostgreSQL (plus the telemetry stack) and
one API process; no Temporal container, namespace job or worker, and nothing
on that startup path imports the Temporal SDK. `retail-analytics-worker`
exits at once (status 3) with an instruction. With `temporal`, dev starts
Temporal and its namespace, the worker and the API, exactly as before;
`/healthz` and `analytics status` report the backend that started.

An environment file from before the setting existed has no
`RETAIL_ANALYTICS_EXECUTION_BACKEND`, so it runs local even though it still
names a Temporal address; the next bootstrap adds `local` and says so.
Temporal settings, containers and volumes are left as they are.

Runs are never moved between backends. Every run records its backend
(`runs.execution_backend`). The API refuses to start while the other backend
still has an active run or a queued request, names the runs and says what to
do: start once with the original backend to let them finish or cancel them
(`./scripts/dev.sh --execution-backend temporal`, then `analytics cancel`), or
stay on it. Nothing is converted, interrupted or reset by switching. Only one
API process with local execution may use a database at a time: a second one
fails at startup.

The Temporal composition (`bootstrap/temporal.py`) is imported only when
`temporal` is selected. The production design keeps Temporal with separately
scaled workers; the local backend is the simpler laptop/demo topology.

## Temporal worker (opt-in)

With `RETAIL_ANALYTICS_EXECUTION_BACKEND=temporal`, after configuring
PostgreSQL and Temporal and running migrations:

```sh
retail-analytics-worker
```

The worker requires `RETAIL_ANALYTICS_DATABASE_URL`,
`RETAIL_ANALYTICS_TEMPORAL_ADDRESS` and the local authentication signing key.
Namespace and task queue use the existing backend settings. In fixture mode
the worker uses an offline model and returns a partial answer without
warehouse findings. In live mode (`RETAIL_ANALYTICS_MODE=live`) it runs the
Gemini primary / GPT backup chain with discovery and guarded query execution;
see [model providers](model-providers.md). The local backend builds the same
model and tools (`bootstrap/execution.py`).

`bootstrap.investigations.build_investigations` composes the application
services and one permission-filtered tool catalog (the same small set on every
model step; nothing depends on an investigation stage):

- `list_relations`, `describe_relation` (with discovery) and
  `execute_analysis` (with guarded query execution);
- `find_analysis_examples` (with a Golden retriever): up to three reviewed
  methods delivered through the knowledge reader for the running catalog
  (`logical-catalog/<n>`) and exact metric versions; illustrative report
  figures are never returned;
- `inspect_preferences`, `remember_preference`, `forget_preference`,
  `confirm_preference`, `decline_preference`: memory passes the output gate
  (destination MEMORY) before it is saved, and an inferred preference is
  confirmed only after a later user message than the proposal;
- `save_report`, `read_report`, `list_reports`, `search_reports`,
  `export_report` and `propose_report_deletion` (with artifact storage):
  saving passes the gate (REPORT) and links the cited evidence to the run;
  deletion is only proposed, the user confirms in the application;
- `convert_currency`, with its declared-currency disclosure.

The model instructions treat the five analytical steps as guidelines, group
products by ID, date order figures by `orders.created_at`, report
contributors rather than causes, and disclose definitions and limitations. An
answer that cites a truncated result is recorded as partial even if the model
claims it is complete.
Tests supply a controlled provider and an external-effect fixture, while still
using real PostgreSQL, Temporal and all application guards.

## Runtime boundary

Investigation behaviour is split from the engine that executes it. Two
execution backends implement it: the durable Temporal workflow and an
in-process local manager (see "Local execution backend" below), selected by
`RETAIL_ANALYTICS_EXECUTION_BACKEND` (see above).

- Application (`application/investigation_runtime.py`): the steps an
  investigation takes - begin, prepare a model step, release an answer, ask,
  resume, finish, cancel - each re-checking authority and budgets and safe to
  repeat. `application/investigation_lifecycle.py` decides what follows each
  step (close, ask, wait for input, restart the agent with rebuilt context,
  stop with partial findings, cancel, expire). It is pure and deterministic.
  Payloads and decisions are plain records in
  `application/contracts/investigations.py`; no Temporal or Pydantic AI type
  enters the application.
- Shared agent (`adapters/agent/investigator.py`): the one Pydantic AI agent,
  its guarded model (fresh authority and context per request, stale-history
  refusal), the permission-filtered catalog toolset and the mapping of its
  output to answer/question drafts. It raises typed outcomes (`RunStopped`,
  `InvestigationContextChanged`, `AgentUnbound`) and imports nothing from
  Temporal; services are passed in through an `AgentBinding`.
- Temporal adapter (`adapters/temporal/`): the workflow interprets lifecycle
  decisions with activities, signals and durable timers; `agent.py` adds
  `TemporalDurability`, activity timeouts/retries, translation of the typed
  outcomes into non-retryable Temporal errors (and back into
  runtime-neutral interruptions) and the per-attempt provider budget key.
  Activity, workflow and error type names, and the payload records, are
  recorded in workflow histories and stay stable.
- Composition: `bootstrap/investigations.py` builds runtime-neutral services
  and binds nothing; `bootstrap/temporal.py` connects the client, creates the
  scheduler and binds and registers the worker for the worker, API and
  evaluation entry points.

Architecture tests reject Temporal imports in the shared agent, model
adapters, the local backend and general/local investigation composition (also
transitive ones), and tests build and run the shared agent and the local
manager in an interpreter where Temporal cannot be imported.

## Local execution backend

`adapters/local/investigations.py` (`LocalInvestigationManager`) runs each
investigation as an asyncio task of the process that opens it - the API
process. It implements the same `InvestigationScheduler` port and carries out
the same lifecycle decisions as the workflow, over the same
`InvestigationRuntime` steps and shared agent; it holds no business rules of
its own. `bootstrap/local_investigations.py` composes it; with local execution
the API opens it in its lifespan (`bootstrap.execution.local_scheduler`):

```python
local = build_local_investigations(settings, persistence, access, model)
async with local.manager:  # lock, orphan sweep, admit ... graceful shutdown
    await local.services.control.start(
        principal, session_id=..., text=..., submission_key=...
    )
```

What it promises, and what it does not:

- Tasks belong to the manager (the application lifespan), never to an HTTP
  request or SSE connection. Clients disconnect and reconnect freely;
  attachment replays persisted events and never starts or restarts work.
  Concurrent agent work is bounded (`RETAIL_ANALYTICS_LOCAL_MAX_CONCURRENT_RUNS`,
  default 4); waiting runs do not hold a slot.
- Starting the same run twice (resubmission, concurrent duplicates, a direct
  scheduler call) never creates a second task. One active run per session,
  steering, queueing, clarification, cancellation, stale-context restarts,
  budgets, output gates and provider fallback are the shared application
  behaviour.
- Clarification waits in memory for a notification that input was persisted;
  no model call and no polling. The wait is bounded by the same seven-day
  limit while the process lives.
- Process lifetime only; no replay. The manager does not retry steps or
  persist agent progress. When the process stops, shutdown stops admission,
  cancels the tasks and, within a bounded grace period
  (`RETAIL_ANALYTICS_LOCAL_SHUTDOWN_GRACE_SECONDS`, default 10 s), ends
  each run as **interrupted**: FAILED (CANCELLED if cancellation was already
  requested) with a notice, the open question closed, pending steering and the
  session's queued requests discarded (kept as history, named in the notice,
  never started). Running warehouse jobs get cancellation requested by their
  recorded job ID; a job that cannot be confirmed stopped is reported as such,
  never as cancelled.
- When the process dies instead, nothing records an outcome at that moment.
  The next manager to open sweeps the orphaned local runs the same way before
  admitting work (`application/investigation_interruption.py`). No model or
  tool call is replayed and no warehouse job is resubmitted. Messages,
  evidence, reports, budgets and operation/job records are kept; the session
  accepts a new request, which the user sends explicitly. This differs from
  Temporal, which resumes an interrupted investigation.
- One manager per database: opening takes a process-lifetime PostgreSQL
  advisory lock and fails clearly when another manager holds it. This is not
  distributed scheduling, leases or leader election; the lock goes away with
  its connection.
- Ownership: every run records its execution backend from creation
  (`runs.execution_backend`, migration 0019; existing runs are `temporal`).
  Local runs store the owning manager instance (`local_execution_id`) and
  never carry Temporal workflow IDs. The local manager refuses to start or
  sweep Temporal runs and their queued requests; the Temporal recovery
  dispatcher ignores local runs and sessions led by a local run.
- Unlike Temporal activities, a failing step is not retried: a run whose step
  fails (for example the database is unavailable) stops with its verified
  findings.

## Recovery boundaries

PostgreSQL stores the authenticated principal ceiling, ordered inputs,
clarification questions, run status, operations, evidence and budgets. Inputs
commit before workflow notifications. A worker dispatcher repairs unsent start,
input and cancellation intents every two seconds. It does not invoke the model
or treat silence as clarification input. Workflow IDs reject duplicate starts,
and operation IDs remain stable across activity retries.

A single active investigation per session is enforced by PostgreSQL. New
messages steer that investigation by default. Explicitly queued requests start
in arrival order after the active investigation ends. Concurrent promotions
converge on the same run, including recovery after starting a workflow but
before marking the queued input promoted.

Clarification uses a durable Temporal signal wait, with a seven-day upper
bound. Waiting status and the active-budget clock change in one transaction.
Disconnecting a client does not cancel the workflow; attachment reads the
persisted status, ordered progress and open question. A pending user message
prevents release of an obsolete answer; the next agent run rebuilds context
using the later request.

Explicit cancellation first makes the investigation refuse new work. The
workflow cancels its running agent task, requests cancellation of recorded
warehouse jobs and reconciles their outcomes for up to two minutes. An
unconfirmed effect remains recorded as unresolved and the cancellation summary
says so. Cancellation never implies that an uncertain effect was rolled back.

## Authority, budgets and payloads

Model and tool activities resolve current authority from the persisted
principal. Model context and evidence rows are rebuilt inside the model
activity, so they are not arguments or results stored in Temporal history.
Tools return compact evidence references rather than result rows. Generated
model messages, tool arguments and final drafts can still appear in the
private Temporal history; its access controls and seven-day closed-history
retention remain necessary. No public traces should copy that content.

Each model response carries application-owned context provenance: a fingerprint
of the authorization version, request, preferences and selected conversation,
plus the evidence IDs and versions it saw. Before sending provider history,
the next activity checks that provenance against freshly authorized context.
Changed context, missing provenance or removed/revised evidence restarts the
agent conversation without sending the old messages. New evidence can be added
without a restart. The investigation keeps its durable budgets and operation
records; a conversation restart does not grant extra budget or undo tool effects.

Answer and clarification release use a fresh output privacy gate and evidence
provenance. A stopped or revoked investigation cannot start new tool work.
Provider requests reserve persistent budget before sending. A worker dying
without receiving usage does not refund its request reservation; a new
activity attempt reserves a distinct request. Actual reported usage settles
that reservation, while uncertain usage retains its estimate. Limits remain
pinned to the run across worker replacement.

Temporal activities use bounded retries and heartbeat timeouts. Model
activities have a 15-minute attempt timeout (a backstop: provider retries and
fallback run inside one activity, each request has its own first-token,
stall and total limits, and the active-time budget refuses new attempts) and
a 30-second heartbeat; tools have a ten-minute attempt timeout and a 15-second
heartbeat. The run's persistent active-time and query deadlines are additional
limits. When every configured provider has failed, the model activity stops
the run as "model unavailable" instead of being retried.

## Validation

```sh
./scripts/check.sh
python -m pytest -m docker tests/integration/test_investigations.py
python -m pytest -m docker tests/integration/test_local_investigations.py \
  tests/integration/test_local_agent_runtime.py
python -m pytest -m docker tests/integration/test_local_http_api.py \
  tests/integration/test_local_cli.py tests/integration/test_dev_up.py
```

`test_local_http_api.py` and `test_local_cli.py` run the HTTP/SSE and CLI
suites of `test_http_api.py` / `test_cli.py` unchanged with local execution
(reconnect with `Last-Event-ID`, attach from a new process, clarification,
cancel, report save/read, typed deletion confirmation and declining it).
`test_dev_up.py` starts `./scripts/dev.sh` on an isolated Compose project:
PostgreSQL only by default, `--execution-backend temporal` for the durable
stack, and a Temporal run left active that blocks local startup until Temporal
finishes it.

The local-backend suites use PostgreSQL only (no Temporal service). They run
the acceptance conversations of `test_agent_runtime.py` unchanged on the local
manager, the lifecycle cases above, shutdown and the one-manager lock, and a
process killed with SIGKILL during a warehouse job, followed by a restart that
records the interruption, reconciles the job by its recorded ID without
resubmitting it, discards the queued request with a notice and accepts a new
request.

The Docker suite creates an isolated PostgreSQL/Temporal stack. It kills a
worker after an external effect commits but before the activity replies,
starts a replacement and verifies one effect plus deterministic history replay.
It also covers clarification restart without model polling, steering, queue
order, cancellation, fresh authority, output privacy and notification recovery.
History assertions inspect decoded payload bytes and bound actual payload
sizes. The fixtures contain controlled test data; these are not live BigQuery
or live provider measurements.
