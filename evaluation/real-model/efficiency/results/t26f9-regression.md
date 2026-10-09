# Conversation efficiency: t26f9-regression

- Recorded 2026-10-09T15:03:27+00:00; code `12c5e1c+dirty`; suite `conversation-efficiency` v1; scoring v3
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 3,000,000 tokens / 500 model attempts; used 167,054 tokens / 18 attempts over 5 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | cold | 1 | 1/1 | 1/1 | 1 | 2 | 10,338 | 11,467 | 11 |
| reuse-evidence | 1 | cold | 1 | 0/1 | 1/1 | 2 | 3 | 17,080 | 20,235 | 30 |
| reuse-evidence | 2 | reuse | 1 | 1/1 | 1/1 | 0 | 1 | 5,357 | 6,787 | 11 |
| why-category-change | 1 | investigation | 1 | 1/1 | 1/1 | 3 | 5 | 41,379 | 52,337 | 81 |
| report-concentration | 1 | investigation | 1 | 1/1 | 1/1 | 4 | 7 | 67,554 | 76,228 | 80 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | 1 | `run_72263c5e391bd76fdabf0dad086fff65` | completed | 1/0/0 | 2 (0, 0) | 10,338/1,129 | 11.2 | 11.2 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| reuse-evidence | 1 | 1 | `run_b8ce43fbfc1adff353f9cd00fdee2a68` | completed | 2/0/0 | 3 (0, 0) | 17,080/3,155 | 30.1 | 30.1 | 0 | 3/3 ev, 3/3 text | MISSED queries | execute_analysis execute_analysis |
| reuse-evidence | 1 | 2 | `run_9eeb4720ba2c51973c542655b5a4c565` | completed | 0/0/0 | 1 (0, 0) | 5,357/1,430 | 11.0 | 11.0 | 0 | 1/2 ev, 2/2 text | met |  |
| why-category-change | 1 | 1 | `run_45583d1807b6791cb8f6e33d4fd87a6d` | completed | 3/0/0 | 5 (0, 0) | 41,379/10,958 | 81.0 | 81.0 | 0 | 6/7 ev, 7/7 text | met | load_skill execute_analysis execute_analysis execute_analysis |
| report-concentration | 1 | 1 | `run_b55e21f9c88c08c6e1ddf0d87d9f9ffe` | completed | 4/1/0 | 7 (0, 0) | 67,554/8,674 | 79.5 | 79.5 | 0 | 3/3 ev, 3/3 text | met | execute_analysis load_skill execute_analysis execute_analysis execute_analysis execute_analysis save_report |
