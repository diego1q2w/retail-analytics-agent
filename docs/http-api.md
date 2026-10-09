# HTTP and SSE API (v1)

`retail-analytics-api` serves the investigation API that the `analytics` CLI
uses. The machine-readable contract is the OpenAPI document at
`GET /openapi.json` (interactive docs at `/docs`), generated from
`src/retail_analytics/interfaces/http/schemas.py`. This page covers the parts
OpenAPI cannot express: authentication, errors, idempotency and the event
stream.

## Starting it

```sh
./scripts/bootstrap.sh                       # once: services, migrations, demo executives
./scripts/dev.sh &                           # the API, http://127.0.0.1:8080 by default
TOKEN="$(retail-analytics-dev-access token local-admin)"
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/v1/sessions
```

The API needs `APP_DATABASE_URL` and
`AUTH_SIGNING_KEY` in every mode, and exits with status 2
naming whichever is missing. The signing key is also a required setting of
live mode.

With local execution (`EXECUTION_BACKEND=local`, the
default) the API process itself runs the investigations; no other service but
PostgreSQL is needed. A run continues when its client disconnects; if the API
stops, its running investigations end as interrupted (see
[investigation runtime](investigation-runtime.md)). Startup fails with a clear
message if another local-execution API already uses the database, or if
Temporal runs are still active.

With Temporal execution (opt-in, `temporal`) the API also needs
`TEMPORAL_ADDRESS` and only schedules; `retail-analytics-worker`
executes (`./scripts/dev.sh --execution-backend temporal` starts both).
Temporal is connected lazily: the API starts before Temporal is ready, and a
request that cannot be scheduled yet answers 503 (retry it with the same
`submission_key`; the worker also re-sends unsent starts).

## Authentication and ownership

Every `/v1` route requires `Authorization: Bearer <token>`. Identity never
comes from a body, query parameter or cookie: request bodies reject unknown
fields, so `executive_id` or similar fields fail validation. Missing, malformed,
expired, forged, wrong-audience or unknown-subject tokens all get the same
401 `unauthenticated` with `WWW-Authenticate: Bearer`.

Everything is owner-scoped. A session, run, event stream, report, export or
deletion proposal that belongs to another executive answers exactly like a
missing one (404 `not_found`). Lists and search only return the caller's own
records. A missing permission (for example no `reports:delete_own`) is 403
`forbidden`.

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Liveness, mode and `execution_backend` (`local` or `temporal`; no authentication) |
| `POST /v1/sessions` | Open a session; body `{submission_key}`, a repeated key returns the same session |
| `GET /v1/sessions` | The caller's sessions, most recently active first (`limit`, `offset`) |
| `GET /v1/sessions/{session_id}` | A session with its runs, newest first |
| `POST /v1/sessions/{session_id}/runs` | Start an investigation `{text, submission_key}`; 409 `active_run_exists` (with `details.active_run_id`) if one is active |
| `POST /v1/sessions/{session_id}/messages` | A message `{text, submission_key, mode}`: `steer` (default) refines the active run or starts one; `queue` waits behind it as a separate request |
| `GET /v1/runs/{run_id}` | Status, the open clarification question and, once ended, the released answer |
| `GET /v1/runs/{run_id}/events` | Server-Sent Events (below) |
| `POST /v1/runs/{run_id}/steer` | Refine the active run `{text, submission_key}` |
| `POST /v1/runs/{run_id}/answers` | Answer the open question `{question_id, text, submission_key}` |
| `POST /v1/runs/{run_id}/cancel` | Stop new work; the run reports `cancelled` once in-flight effects settle |
| `GET /v1/reports` | The caller's saved reports (`session_id`, `limit`, `offset`) |
| `GET /v1/reports/search?q=` | Search titles and content (`session_id`, `limit` up to 25) |
| `GET /v1/reports/{report_id}` | Read a report and its cited evidence (`version` optional); `definition_notices` lists where the definitions recorded as relevant to its evidence differ from your current ones, or were not recorded (display-time, never part of the report; context from the fields the queries read, not proof the SQL implemented a metric) |
| `GET /v1/reports/{report_id}/versions` | Its versions |
| `GET /v1/reports/{report_id}/export` | One Markdown file with the evidence appendix (`text/markdown` attachment); definition notices, if any, in the `X-Report-Definition-Notices` header (JSON list) |
| `GET /v1/deletion-proposals?status=pending` | The caller's own pending, unexpired proposals, newest first: `{"proposals": [<preview>]}`. `pending` is the only status (default); anything else is 422. Confirmed, cancelled and expired proposals and other executives' proposals never appear. Listing deletes nothing |
| `GET /v1/deletion-proposals/{proposal_id}` | Preview a deletion the assistant proposed |
| `POST /v1/deletion-proposals/{proposal_id}/confirm` | Delete exactly the proposed reports; body must be `{"confirm": true}` |
| `POST /v1/deletion-proposals/{proposal_id}/cancel` | Withdraw the proposal |
| `GET /v1/persona` | The active company persona (editors, `persona:edit`; `current: null` when none) |
| `GET /v1/persona/history` | Versions and every publish/rollback, newest first (`limit`) |
| `POST /v1/persona/drafts` | Draft from free text `{content, submission_key}`; personal data is 422 `sensitive_content`; text that conflicts with fixed policy is stored with `findings` but cannot be previewed or published (422 `policy_conflict`) |
| `PUT /v1/persona/drafts/{id}` | Replace your own draft `{content, expected_revision}` (409 `conflict` if stale); clears its preview |
| `DELETE /v1/persona/drafts/{id}` | Discard your own draft |
| `POST /v1/persona/drafts/{id}/preview` | Current and proposed persona over the same sample findings; checks figures, evidence and limitations survived |
| `POST /v1/persona/drafts/{id}/publish` | `{expected_current_version_id}` (the active version you saw, or null). 409 `conflict` if it moved or the draft is based on an older version, 409 `not_previewed` without a preview of this exact text |
| `POST /v1/persona/rollback` | `{target_version_id, expected_current_version_id}`: make an earlier published version active again |

Period note: a query that compares several periods currently has no single recorded period (there is no multi-period marker), so a report or agent answer should disclose the periods it compares.

### Answer citations

A released answer (`answer` in `GET /v1/runs/{run_id}`) keeps the evidence IDs
it cites in its `text` (for example `[evd_90c4…]`) and adds `citations`: one
entry per recognized evidence record, in order of first use:

```json
{"number": 1, "label": "1", "evidence_id": "evd_90c4…", "kind": "query",
 "description": "Query result; definition basis: completed item sales v1; September 2026 (UTC, by ordered date); computed 9 October 2026, 14:02 UTC.",
 "current": true}
```

- Computed when the answer is read, under the caller's current access. An ID
  is listed only if it names a record the answer's run used and that the
  caller may use now. An invented ID, another executive's or session's record,
  or a record withheld after access narrowed (including a deleted report's
  link) is not listed and stays in the text as written. A withheld answer
  always has an empty list. A superseded record (a definition or preference
  changed after it was computed) is listed with `current: false`.
- `description` is written by the application from recorded metadata (kind,
  definition basis, period, time zone and date field, grouping, computation
  time, truncation, saved-report source or exchange-rate provenance). It never
  contains SQL, product entitlements, credentials or rows, and no model writes
  it. Metadata that an older record does not have is described as not
  recorded. Descriptions pass the same output check as the answer. If one
  cannot be shown, a neutral line replaces it.
- `label` is what to display: `1`, `2`, …, or `S1`, `S2`, … when the answer
  already has numbered references of its own such as `[1]`. Clients replace
  only the listed IDs, and never inside code, URLs or longer words.
  Clarification questions never have citations.

Deletion confirmation exists only as this explicit, authenticated user
request: the model can propose a deletion but nothing it writes can confirm
one. There is no restore route. Restoring a deleted report within its seven
days is an operator action (`retail-analytics-maintenance restore`).

## Idempotency

Every write carries a client `submission_key` (1–128 characters, letters,
digits, `.`, `_`, `:`, `-`). Repeating a request with the same key returns
the original result (`created: false` for a run) and never starts a second
run; reusing a key for different content is 409 `idempotency_conflict`.
After a 503 or a lost response, resend the same request with the same key.

## Errors

Every error has the same body:

```json
{"error": {"code": "run_not_active", "message": "...", "details": {}}}
```

| Status | Codes |
| --- | --- |
| 400 | `invalid_cursor` (malformed `Last-Event-ID`, or one that is not this run's) |
| 401 | `unauthenticated` |
| 403 | `forbidden` |
| 404 | `not_found` (missing or not the caller's) |
| 409 | `active_run_exists`, `run_not_active`, `idempotency_conflict`, `access_changed`, `evidence_unavailable`, `stale_base_version`, `already_resolved`, `stale`, `too_many_pending` |
| 410 | `expired` (deletion proposal older than ten minutes) |
| 422 | `invalid_request` (malformed body or parameters; `details.problems` lists locations, never the submitted values) |
| 503 | `unavailable` (retry with the same `submission_key`) |

## Event stream

`GET /v1/runs/{run_id}/events` returns `text/event-stream`. Each event is one
persisted progress event of the run:

```text
id: 8a9290d810bb4402a2dce2bd89bdb724
event: input.required
data: {"schema_version":1,"correlation":{...},"kind":"input.required","source":"application","summary":"Waiting for your answer.","tool":null,"input_request":{"question_id":"qst_...","question":"Which sales period should I use?"},"event_id":"8a92...","sequence":2,"occurred_at":"2026-10-09T04:11:59Z"}
```

`data` is a `ProgressEvent` (`application/contracts/progress.py`): `kind` is
one of `run.started`, `analysis.progress`, `tool.started`, `tool.retrying`,
`tool.pending`, `tool.outcome_unknown`, `tool.succeeded`, `tool.failed`,
`input.required`, `input.applied`, `input.not_applied`, `deletion.proposed`,
`run.completed`, `run.partial`,
`run.failed`, `run.cancelled`; `sequence` starts at 1 and increases by one per event.

- **Deletion proposals.** When the assistant proposes deleting reports, the
  run gets one `deletion.proposed` event whose only structured payload is
  `deletion_proposal_id` (no titles, no report IDs; the summary is fixed text).
  Clients fetch the proposal with `GET /v1/deletion-proposals/{id}` or list
  the pending ones; they never parse answer text for IDs. A retried proposal
  repeats the event with the same ID. The event confirms nothing.
- **Resume.** Reconnect with the `Last-Event-ID` header (or `?after=<event_id>`)
  set to the last ID received: the stream continues with exactly the later
  events, in order, with no gaps or repeats. Connecting or reconnecting never
  starts, restarts or cancels anything; disconnecting leaves the run working.
- **Release.** Every streamed summary and question passes the output privacy
  gate, with the caller's authority resolved again for every batch: personal
  data is masked, and a summary the gate refuses is replaced by a fixed
  notice, never dropped, so sequences stay whole. The stream re-checks
  ownership on every poll; if access is lost it sends
  `event: error` (`{"code": "not_found", ...}`) and closes.
- **Keep-alive.** Comment lines (`: keepalive`) are sent when nothing happened
  for 15 seconds. The server suggests `retry: 2000` milliseconds.
- **End.** After the run's terminal event the server sends
  `event: end` with `{"run_id", "status"}` (no ID) and closes. Then
  `GET /v1/runs/{run_id}` returns the released answer. A connection is also
  closed after 15 minutes; reconnect with the last ID.
- **Steering outcome.** An accepted steering message (`202`, `kind:
  "steering"`) is pending until the run's next model step. `input.applied`
  confirms it reached the investigation; if the run ends first,
  `input.not_applied` precedes the terminal event and the released answer
  ends with a notice quoting it. It is never silently dropped.
- **Questions.** On `input.required`, answer with
  `POST /v1/runs/{run_id}/answers` using `input_request.question_id`; the
  stream (still open, or resumed) then continues.
