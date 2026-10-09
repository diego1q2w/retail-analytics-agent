# Conversation efficiency: t26f7-currency-rate

- Recorded 2026-10-09T13:34:23+00:00; code `4dd3b1a`; suite `currency-request` v1; scoring v2
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 300,000 tokens / 60 model attempts; used 29,077 tokens / 5 attempts over 1 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| currency-request | 1 | cold | 1 | 0/1 | 1/1 | 1 | 5 | 26,001 | 29,077 | 43 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| currency-request | 1 | 1 | `run_a4333774c806fab9cc58a4f5eca0357b` | completed | 1/0/0 | 5 (0, 0) | 26,001/3,076 | 43.0 | 43.1 | 0 | 1/1 ev, 1/1 text | MISSED no_unexpected_question | execute_analysis load_skill convert_currency convert_currency |
