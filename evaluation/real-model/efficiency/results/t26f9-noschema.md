# Conversation efficiency: t26f9-noschema

- Recorded 2026-10-09T14:59:51+00:00; code `12c5e1c+dirty`; suite `intended-question` v1; scoring v3
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 1,500,000 tokens / 300 model attempts; used 14,201 tokens / 3 attempts over 1 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| overview-missing-schema | 1 | cold | 1 | 1/1 | 0/1 | 0 | 3 | 12,790 | 14,201 | 18 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| overview-missing-schema | 1 | 1 | `run_0bc53102468631c07000e2df73c3172a` | completed | 0/0/0 | 3 (0, 0) | 12,790/1,411 | 18.0 | 18.0 | 0 | - | met | list_relations describe_relation |
