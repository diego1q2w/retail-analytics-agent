# The `analytics` command-line client

`analytics` is the prototype user interface. It only talks to the HTTP API
(see [HTTP and SSE API](http-api.md)); it holds no database, model or cloud
credentials and imports nothing from the backend beyond its own settings.

## Setup

```sh
./scripts/bootstrap.sh
./scripts/dev.sh &        # the API (local execution: it runs the investigations)
retail-analytics-dev-access token demo-a > ~/.analytics-token && chmod 600 ~/.analytics-token
export ANALYTICS_CLI_TOKEN_FILE=~/.analytics-token     # or ANALYTICS_CLI_TOKEN=...
analytics chat
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `ANALYTICS_CLI_API_URL` | `http://127.0.0.1:8080` | Backend address |
| `ANALYTICS_CLI_TOKEN` / `ANALYTICS_CLI_TOKEN_FILE` | none | Bearer token; the file wins and is re-read on every start. The token is only ever sent as the `Authorization` header and is never printed or put in error text. |
| `ANALYTICS_CLI_TIMEOUT_SECONDS` | `10` | Per-request timeout (event streams use their own stall limit) |

## Interactive chat

`analytics chat [--session ID | --resume]` opens a session (a new one by
default), or reopens one: a pending question and the run's progress are
shown again, and the last answer if nothing is running.

```text
$ analytics chat
Session 6f1c...  Ask a question; /help lists commands, /quit leaves.
you> How did Women's revenue trend last quarter?
Working on it.
  > Looking up the available data.
  > Running an analysis query.
  ok Query finished.
The assistant needs an answer to continue:
  Which sales period should I use?
answer> the last full calendar quarter
Revenue ...
== DISCLOSURES ==
  ...
== NEXT STEPS ==
  [ ] ...
you> /reports
```

While a run works you can keep typing (on a terminal): plain text steers the
run, or answers its open question; `/queue <text>` queues a separate question
that starts when the run ends; `/cancel` cancels; `/status` and `/follow`
show or re-attach to progress. Other commands: `/sessions`, `/new`,
`/reports`, `/search <words>`, `/report <id> [version]`,
`/export <id> [file]`, `/confirm <proposal>`, `/decline <proposal>`,
`/help`, `/quit`. Malformed or unknown commands and any backend error print
one error line and the chat continues.

When stdin is not a terminal (scripts, tests) each line is sent only after
the previous run has finished or asked a question, so a script is
deterministic.

### Ctrl-C and closing

* While a run is followed, Ctrl-C only **detaches**: the run keeps working on
  the server and nothing is cancelled. Closing the terminal or `/quit` does
  the same. `/follow` re-attaches; `--resume` / `--session` reopens later.
* At an idle prompt Ctrl-C leaves the chat.
* During a deletion confirmation Ctrl-C means "not confirmed".
* `/cancel` (or `analytics cancel RUN`) stops new work. The run is reported
  `cancelling` until in-flight external work (a running query) is confirmed
  stopped, and only then `cancelled`; the CLI says so instead of claiming it is
  stopped.

### Reading results

* Progress shows tool and analysis steps only (no private reasoning exists in
  the stream). Retries, pending and outcome-unknown steps are marked.
* **Partial** answers start with a `PARTIAL RESULT` banner; truncated report
  evidence is flagged `TRUNCATED`; an answer withheld by the privacy check
  is replaced by the server's notice.
* Markdown headings, lists, bold and code are rendered; sections that look
  like disclosures/assumptions are marked `== ... ==` in yellow and action
  items / next steps in green with `[ ]` checkboxes.
* All server text has terminal escape sequences and control characters
  removed before display.

### Connection handling

The event stream reconnects by itself with `Last-Event-ID` after a dropped
connection, a stall (no event or keepalive for 45 s) or the server's 15-minute
limit; events already shown are never repeated. After 8 failed attempts in a
row it stops and prints the `--after` value to resume with. Every write
carries a `submission_key`; on a lost response or HTTP 503 the same request
is resent with the same key (a second run is never started). The only write
that is not blindly resent is a deletion confirmation: if its response is
lost the CLI says the outcome is unknown and how to check.

## Deleting reports

The assistant can only *propose* deleting reports. When it does, the server
announces the proposal on the run (a `deletion.proposed` event with the
proposal ID) and lists it among your pending proposals
(`GET /v1/deletion-proposals?status=pending`). The chat shows the server's own
record of each pending proposal once (every report ID, version, title and
date), right when the event arrives and again after the run for any proposal
it has not shown yet. The answer text is never scanned for IDs, and nothing is
ever confirmed automatically. To delete:

```text
you> /confirm 3c9a...
Deletion proposal 3c9a... (pending): 2 report(s) would be deleted
  - rep_01  v2  saved 2026-10-01  Client X revenue
  - rep_02  v1  saved 2026-10-02  Client X churn
Type "delete 2 reports" to delete exactly these reports, or press Enter to keep them:
```

Only the exact phrase for that count deletes; anything else (including `y`
or `yes`) changes nothing. There is no `--yes` flag. Scripts must pass the
phrase explicitly: `analytics deletion confirm ID --confirm-text "delete 2 reports"`
(and so must have read the preview). Restoring within seven days is an
operator action (`retail-analytics-maintenance restore`), not a CLI command.

## Scriptable commands

All print errors as `error [code]: message` (stderr or merged output) and
exit 1; usage errors exit 2.

| Command | Purpose |
| --- | --- |
| `ask TEXT [--session ID] [--answer TEXT]... [--submission-key K] [--no-wait] [--json]` | Start a run and follow it; answers are used in order for clarifications |
| `follow RUN [--after EVENT_ID] [--answer TEXT]...` | Attach to a run from any process |
| `answer RUN TEXT [--question-id ID]` | Answer the open question, then follow |
| `steer RUN TEXT`, `queue SESSION TEXT` | Refine the active run / queue a separate question |
| `cancel RUN [--wait]` | Cancel |
| `show RUN`, `runs SESSION`, `sessions` | Inspect state (`--json`) |
| `reports list\|search Q\|show ID [--version N]\|versions ID\|export ID [-o FILE]` | Saved reports; `show` and `export` print a `DEFINITIONS:` line when the definitions recorded as relevant to a report's evidence differ from your current ones or were not recorded (context, not proof the SQL implemented a metric; the saved report is unchanged) |
| `deletion list` | Your pending, unexpired proposals (`--json`) |
| `deletion show\|confirm\|cancel PROPOSAL` | Review and decide on a proposal |
| `status` | Backend health |

Period note: a query that compares several periods currently has no single recorded period (there is no multi-period marker), so a report or agent answer should disclose the periods it compares.

Exit codes of `ask`, `follow`, `answer`, `cancel`: 0 completed, 3 partial
result, 4 waiting for your answer, 5 failed, 6 cancelled, 7 cancellation
still being confirmed. With the same `--submission-key`, re-running `ask`
returns to the same session and run, so a script can safely retry after a
crash. With `--json` progress goes to stderr and stdout holds one JSON object.
