# Model providers

The investigation agent uses one model chain: Gemini as the primary and a GPT
model as the backup. The application owns the routing, the retries and the
accounting; the model cannot choose a provider or relax any policy.

## Selected models

| Role | Default | API | Setting |
| --- | --- | --- | --- |
| Primary | `gemini-3.8-flash` | Gemini Interactions API (`POST /v1beta/interactions`) | `RETAIL_ANALYTICS_AGENT_GEMINI_MODEL` |
| Backup | `gpt-5-mini` | OpenAI Responses API | `RETAIL_ANALYTICS_AGENT_OPENAI_MODEL` |

- **Gemini 3.8 Flash** is listed as stable in Google's model catalog
  ("our most intelligent Flash model, engineered for long-horizon software
  engineering, autonomous agents, and complex enterprise workflows";
  <https://ai.google.dev/gemini-api/docs/models>). It is the primary because
  the agent is a multi-step tool-calling loop. With the project's key, the
  model answered on the Interactions API and returned 404 on `generateContent`
  (checked on 2026-10-09).
- **GPT-5 mini** is OpenAI's cost-efficient GPT-5 variant. It supports
  function calling, streaming and structured outputs on the Responses API, has
  a 400k-token context window, and costs $0.25/$2 per 1M input/output tokens
  (<https://developers.openai.com/api/docs/models/gpt-5-mini>). It was
  reachable with the project's key. `gpt-5.4-mini` is a stronger and more
  expensive alternative ($0.75/$4.5) that uses the same adapter. To switch,
  change the setting.

Changing either model is a configuration change. The Interactions adapter
works for any Gemini model that the Interactions API serves (for example
`gemini-3-flash-preview`). The backup is enabled only when
`RETAIL_ANALYTICS_OPENAI_API_KEY` is set. Without that key there is no
fallback, and a primary outage stops the run with its verified findings.

### Why a custom Gemini adapter

Pydantic AI 2.54 (the latest release at implementation time) calls Gemini
through `generateContent` only, so it cannot reach `gemini-3.8-flash`. The
adapter `adapters/models/gemini_interactions.py` is a small Pydantic AI
`Model` with these properties:

- **Scope.** It handles text prompts, the permission-filtered function tools
  plus the structured-answer tools, and text and function-call output,
  streamed over server-sent events.
- **Stateless requests.** Every request sends `store: false` and the full
  application-owned history. Nothing is kept on Google's side between turns.
- **Thought signatures.** Gemini requires the opaque signature it attached to
  each of its own function calls to be sent back; without it the API answers
  400. The adapter echoes those signatures. A call another provider made
  (after a fallback) carries Google's documented placeholder
  `skip_thought_signature_validator`. Reasoning text is never requested,
  stored or forwarded.
- **Secrets and errors.** The key goes in the `x-goog-api-key` header, never
  in the URL. Errors keep only the provider's code, message and retry hint.

The OpenAI backup uses Pydantic AI's native `OpenAIResponsesModel`. It sends
`store: false` and keeps reasoning only as encrypted content.

## Request policy

Each provider request is streamed, even when the agent needs a whole response,
so that a slow start can be told apart from a long answer:

| Limit | Default | Setting |
| --- | --- | --- |
| First streamed token | 60 s | `RETAIL_ANALYTICS_MODEL_FIRST_TOKEN_SECONDS` (5–600) |
| Stall between streamed events | 30 s | `RETAIL_ANALYTICS_MODEL_STREAM_STALL_SECONDS` (5–600) |
| Whole request | 180 s | `RETAIL_ANALYTICS_MODEL_REQUEST_MAX_SECONDS` (30–1800) |
| Output tokens per request (reasoning included) | 8192 | `RETAIL_ANALYTICS_MODEL_MAX_OUTPUT_TOKENS` |
| Primary skipped after it failed | 60 s | `RETAIL_ANALYTICS_MODEL_PRIMARY_COOLDOWN_SECONDS` |

- **Timeouts.** A response that keeps streaming is not cut off at 60 seconds;
  only the stall and total limits apply after the first token. Any streamed
  event counts as progress: text, tool-call arguments or a reasoning
  signature. The run's active-time budget (10 minutes) bounds the
  investigation as a whole.
- **Retries.** Retries cover throttling (429), server errors (5xx, 408/409),
  connection failures and the timeouts above. They use the run budget's
  backoff with jitter and honour the provider's retry hint. There are at most
  `RETAIL_ANALYTICS_MAX_TRANSIENT_ATTEMPTS` (3) attempts per provider for one
  model request, and none once the run's active time is nearly spent.
- **Long retry hints.** If the hint is longer than
  `RETAIL_ANALYTICS_RETRY_MAX_SECONDS`, the request does not wait. It moves
  to the backup.
- **Fallback.** When the primary has spent its attempts, or rejects the key
  or the model (401/403/404), the request goes to the backup. The primary
  then cools down: for that period, requests from every run in the worker go
  straight to the backup without probing it again.
- **No retry.** Other rejections (for example 400) fall back once and are
  not retried.
- **Both failed.** When both providers fail, the run stops as "model
  unavailable" and shows its verified findings. It is not retried as a whole,
  so retries never multiply across layers. The SDK's own retries are turned
  off: `max_retries=0` for OpenAI, and the Gemini adapter makes no retries.

### What a fallback preserves

A fallback repeats only the model request. Tool calls are separate durable
activities, recorded once with their effects, so the backup never re-runs a
completed query. The backup receives the same application-built history: the
system instructions, the user request, earlier tool calls with their results
(evidence references), and the same permission-filtered tool catalog.
Before a request is sent, any reasoning produced by the other provider is
removed from that history, so one provider's private reasoning never reaches
the other.

## Run budget accounting

Every request that actually leaves the process is counted against the run's
persistent budget (20 provider requests and 100k tokens by default; see
"Run budgets and recovery" in the README). This includes in-activity
retries, fallback attempts and Temporal activity retries.

- **Reserve first.** Each request reserves budget before it is sent, using a
  key unique to that attempt. A refused reservation stops the run; the
  request is not sent.
- **Reported usage settles the charge.** For Gemini: input plus tool-use
  prompt tokens, and output plus thinking tokens. For OpenAI: input and
  output tokens.
- **Missing usage is ambiguous.** A cut-off stream, a timeout, a lost
  response or a server error leaves the estimate in place. The charge is
  marked ambiguous and is never refunded.
- **Definite rejections.** A 4xx such as 429 settles at zero tokens, but
  still counts as a request.
- **Cooling primary.** While the primary cools down, it is skipped without a
  request and without a charge.

## Rate limits

- **Gemini.** Limits are per project, measured as requests per minute,
  input tokens per minute and requests per day. Exceeding any of them
  returns `429 RESOURCE_EXHAUSTED`. Your project's actual numbers are shown
  in AI Studio (<https://ai.google.dev/gemini-api/docs/rate-limits>). A key
  from a project without billing is on the free tier, so its limits are low.
  On 2026-10-09 the free tier allowed `gemini-3.8-flash` 20 requests per
  day; once they were spent it answered 429 with a retry hint of about 20
  hours, and requests went to the backup. One investigation may use up to 20
  provider requests, so a single run can spend a whole day's free quota.
  For regular use, enable billing on the AI Studio project. During
  implementation the primary also answered `503` ("experiencing high
  demand") at times; the fallback covers that too.
- **OpenAI.** `gpt-5-mini` is not available on the free tier. Tier 1 allows
  500 requests and 500k tokens per minute
  (<https://developers.openai.com/api/docs/models/gpt-5-mini>).

## Configuration

Set these in `.env` (live mode):

```sh
RETAIL_ANALYTICS_MODE=live
RETAIL_ANALYTICS_GEMINI_API_KEY=<AI Studio key>
RETAIL_ANALYTICS_OPENAI_API_KEY=<OpenAI key>   # optional: enables the backup
```

The keys need the `RETAIL_ANALYTICS_` prefix; unprefixed `OPENAI_API_KEY` or
`GEMINI_API_KEY` variables are not read. In live mode, `retail-analytics-worker`
builds the chain, discovery and guarded query execution. Fixture mode keeps the
offline model.

## Tests

- **`tests/unit/models/`** runs the real Gemini and OpenAI adapters against
  HTTP stubs:
  - request mapping, signatures, a sanitized 429 and retry hint;
  - fallback that keeps context without repeating a tool;
  - cooldown, budget refusal before sending, and both providers failing;
  - reasoning isolation, and keys that never appear in errors.
- **Fake-clock tests** check the first-token, stall and total limits.
- **`tests/live/test_model_providers_live.py`** (marker `live`; a handful of
  requests) runs Gemini alone, a fallback from an overloaded Gemini to GPT,
  and Gemini continuing a conversation GPT started. When Gemini answers 429
  (quota spent), the scenario is reported as skipped, not passed.
