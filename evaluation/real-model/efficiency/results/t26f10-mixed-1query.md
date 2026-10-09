# Conversation efficiency: t26f10-mixed-1query

- Recorded 2026-10-09T16:56:05+00:00; code `43346e8`; suite `resolved-refusals` v1; scoring v4
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 400,000 tokens / 80 model attempts; used 32,479 tokens / 3 attempts over 1 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mixed-unfinished-permitted-work | 1 | cold | 1 | 0/1 | 0/1 | 1 | 3 | 23,771 | 32,479 | 72 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mixed-unfinished-permitted-work | 1 | 1 | `run_83624eb247940d43a953c725abc21039` | partial | 1/0/0 | 3 (0, 0) | 23,771/8,708 | 71.8 | 71.9 | 0 | - | MISSED completed | fetch_evidence execute_analysis |
