# Conversation efficiency: t26f7-currency-norate

- Recorded 2026-10-09T13:35:10+00:00; code `4dd3b1a`; suite `currency-request` v1; scoring v2
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 300,000 tokens / 60 model attempts; used 30,754 tokens / 5 attempts over 1 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| currency-request | 1 | cold | 1 | 0/1 | 1/1 | 1 | 5 | 26,217 | 30,754 | 44 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| currency-request | 1 | 1 | `run_eeaca9cb557f910811da936f90ad0e2e` | partial | 1/0/0 | 5 (0, 0) | 26,217/4,537 | 44.5 | 44.6 | 0 | 1/1 ev, 1/1 text | MISSED completed,no_unexpected_question | execute_analysis load_skill convert_currency convert_currency |
