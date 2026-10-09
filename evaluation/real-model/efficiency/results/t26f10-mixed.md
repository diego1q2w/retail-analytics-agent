# Conversation efficiency: t26f10-mixed

- Recorded 2026-10-09T16:54:51+00:00; code `43346e8`; suite `resolved-refusals` v1; scoring v4
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 400,000 tokens / 80 model attempts; used 61,844 tokens / 7 attempts over 1 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mixed-unfinished-permitted-work | 1 | cold | 1 | 1/1 | 0/1 | 6 | 7 | 55,063 | 61,844 | 67 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mixed-unfinished-permitted-work | 1 | 1 | `run_c37e4ae1d505fbf024e5d535524d0fb1` | completed | 6/0/0 | 7 (0, 0) | 55,063/6,781 | 67.0 | 67.0 | 0 | - | met | execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis |
