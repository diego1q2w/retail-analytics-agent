# Durable investigation runtime

Each investigation runs as one Pydantic AI agent inside a Temporal workflow.
The agent can explore, call tools, ask a clarification or produce an answer.
Analytical guidelines do not impose a fixed sequence of stages.

## Local worker

After configuring PostgreSQL and Temporal and running migrations:

```sh
retail-analytics-worker
```

The worker requires `RETAIL_ANALYTICS_DATABASE_URL`,
`RETAIL_ANALYTICS_TEMPORAL_ADDRESS` and the local authentication signing key.
Namespace and task queue use the existing backend settings. In fixture mode
the worker uses an offline model and returns a partial answer without
warehouse findings. In live mode (`RETAIL_ANALYTICS_MODE=live`) it runs the
Gemini primary / GPT backup chain with discovery and guarded query execution;
see [model providers](model-providers.md).

`bootstrap.investigations.build_investigations` composes the application
services. An integration can supply discovery, guarded query execution and a
provider model; the composition root registers `list_relations`,
`describe_relation` and `execute_analysis` when their services are supplied.
Tests supply a controlled provider and an external-effect fixture, while still
using real PostgreSQL, Temporal and all application guards.

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
```

The Docker suite creates an isolated PostgreSQL/Temporal stack. It kills a
worker after an external effect commits but before the activity replies,
starts a replacement and verifies one effect plus deterministic history replay.
It also covers clarification restart without model polling, steering, queue
order, cancellation, fresh authority, output privacy and notification recovery.
History assertions inspect decoded payload bytes and bound actual payload
sizes. The fixtures contain controlled test data; these are not live BigQuery
or live provider measurements.
