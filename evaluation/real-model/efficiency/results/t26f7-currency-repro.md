# Conversation efficiency: t26f7-currency-repro

- Recorded 2026-10-09T13:31:02+00:00; code `4dd3b1a`; suite `analytical-skills` v1; scoring v2
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 700,000 tokens / 140 model attempts; used 41,490 tokens / 7 attempts over 1 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| currency-request | 1 | cold | 1 | 0/1 | 1/1 | 1 | 7 | 37,127 | 41,490 | 42 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| currency-request | 1 | 1 | `run_5685a321ea8d605ba91c74240e48418c` | partial | 1/0/0 | 7 (0, 0) | 37,127/4,363 | 42.1 | 42.3 | 0 | 1/1 ev, 1/1 text | MISSED completed,no_unexpected_question | execute_analysis load_skill convert_currency convert_currency convert_currency convert_currency |
