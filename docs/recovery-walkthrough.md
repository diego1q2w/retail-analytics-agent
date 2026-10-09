# Recovery and CLI walkthrough (human, about 25 minutes)

This is a checklist for a person to run by hand and record. It has **not
been performed yet**: every "Result" cell below is empty until
someone runs it. It does not claim exhaustive resilience; it covers one
representative path per behaviour.

## What the automated tests already cover (do not repeat by hand)

| Behaviour | Local execution (default) | Temporal execution (opt-in) |
| --- | --- | --- |
| CLI disconnect, run keeps going, new process re-attaches | `tests/integration/test_local_cli.py` (Docker) | `tests/integration/test_cli.py` (Docker) |
| SSE reconnect with `Last-Event-ID`, no repeated events; bounded reconnects after a `retry:` preamble | same two files; `tests/unit/cli/test_follow.py` | same |
| Clarification, cancel, typed deletion confirmation | `test_local_cli.py` | `test_cli.py` |
| Backend process ends | run ends **interrupted**, queued request discarded, nothing re-run: `tests/integration/test_local_investigations.py` (graceful shutdown), `tests/integration/test_local_agent_runtime.py` (SIGKILL, marked at next start), `tests/unit/cli/test_commands.py` (CLI shows it as failed and never restarts it) | the workflow resumes on a worker, committed effects are not repeated: `tests/integration/test_investigations.py` (worker kill, restart during clarification) |
| Uncertain BigQuery job reconciled, no duplicate submission | `tests/unit/query_execution/` (both backends share it) | same |
| Provider retry and fallback, Gemini stream cut mid-stream, uncertain attempt charged once | `tests/unit/models/test_provider_chain.py`, `test_gemini_interactions.py`, `tests/unit/test_investigation_models.py`; `test_local_investigations.py::test_budget_stop_and_model_fallback_use_shared_policy` | same |

Recovery semantics, stated plainly:

* **Local (default).** Closing or crashing the CLI never affects the run.
  Stopping the API gracefully records the interruption at once. If the API is
  killed, the run is marked interrupted **at the next API start**. Nothing is
  resumed or replayed; you send the request again. There is no durable
  recovery in this mode.
* **Temporal (opt-in, `EXECUTION_BACKEND=temporal`).** A killed worker or API
  does not lose the run: the workflow resumes on a worker. Not measured here
  by hand; the evidence is the Docker tests above.

## Preparation

```sh
./scripts/bootstrap.sh
./scripts/dev.sh                     # leave running in terminal A
retail-analytics-dev-access token demo-a > ~/.analytics-token && chmod 600 ~/.analytics-token
export CLI_TOKEN_FILE=~/.analytics-token     # in terminal B (and every new terminal)
analytics status                     # expect: backend healthy, execution backend "local"
```

Steps 4 and 5 need a model that can save and propose deleting reports. The
offline fixture model cannot, so use live mode (BigQuery and Gemini
credentials in `.env`, see [Google access setup](google-access.md)); in
fixture mode do steps 1 to 3 and 6 to 8 and mark 4 and 5 "not testable".

Record: reviewer, date, `APP_MODE`, execution backend, git SHA.

## Steps

Run in terminal B unless stated. "Expected" is the shape, not exact text.

| # | Do | Expected | Result / friction |
| --- | --- | --- | --- |
| 1 Ask | `analytics chat`, then type a question, e.g. `How did revenue trend by month last year?` | `Working on it.`, indented progress lines (`>` tool step, `ok` finished), then an answer with `== DISCLOSURES ==` and `== NEXT STEPS ==` sections | |
| 2 Follow-up | In the same chat: `And only for Women's products?` | A new run in the same session; the answer refers to the earlier one | |
| 3 Clarification | Ask something ambiguous, e.g. `Show me sales`. | `The assistant needs an answer to continue:` and an `answer>` prompt. Answer it (`last full calendar quarter`); the run continues and finishes | |
| 4 Report | `Save that analysis as a report`, then `/reports` and `/report <id>` | A report id appears in the list; `/report` shows its content | |
| 5 Delete | `Delete the report <title>`, then `/confirm <proposal-id>` | Preview lists the report; first type `no` or press Enter: nothing deleted (`/reports` still lists it). Repeat and type the exact phrase shown (`delete 1 report`): it is deleted | |
| 6 Disconnect and resume | Ask a long question (e.g. `Compare revenue by brand, category and month for the last 3 years`). While it works press Ctrl-C (detaches; the run is not cancelled). Then `analytics runs <session>` and `analytics follow <run-id>`, or `analytics chat --resume` | Chat says it detached; `follow` replays missed progress with no repeated lines and the run finishes | |
| 7 Cancel | Start another long question, then `/cancel` | `cancelling` is reported until the query is confirmed stopped, then `cancelled` (exit code 6 via `analytics cancel`) | |
| 8a Backend killed (local) | Start a long question with `analytics ask "<long question>" --no-wait`. In terminal A press Ctrl-C **or**, for the harder case, `kill -9` the API pid shown by `dev.sh`. Restart `./scripts/dev.sh`. Then `analytics show <run-id>` and `analytics follow <run-id>` | Status `failed` with the message that the analysis service stopped, nothing was resumed, send the request again (for `kill -9` this appears only after the restart). Exit code 5. Re-ask: a new run completes | |
| 8b Backend killed (Temporal, optional) | Needs `./scripts/dev.sh --execution-backend temporal`. Start a long question, `kill -9` the worker pid, restart dev | Run resumes and completes once; no duplicate side effects | |

## What to record

For each step write: pass, fail, or not tested; what you saw if it differed
from "Expected"; anything confusing (wording, missing hint, slow). Do not
edit the database or use anything other than the commands above. If a step
is blocked (missing credentials, a service that will not start), write
"blocked" and why; a blocked step is not a pass.

```text
Reviewer:            Date:
Mode / backend / SHA:
1 Ask:               2 Follow-up:         3 Clarification:
4 Report:            5 Delete:            6 Disconnect/resume:
7 Cancel:            8a Local kill:       8b Temporal kill:
Friction observed:
Untested / blocked:
```

Optional publishing needs no second account in the local-admin setup; it is
not part of this checklist.
