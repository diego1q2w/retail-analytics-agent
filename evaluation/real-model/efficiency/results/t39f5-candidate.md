# Conversation efficiency: t39f5-candidate

- Recorded 2026-10-09T12:43:30+00:00; code `b318902`; suite `conversation-efficiency` v1; scoring v2
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 3,000,000 tokens / 500 model attempts; used 84,947 tokens / 15 attempts over 9 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 9,049 (9,030-9,185) | 10,404 (10,364-10,595) | 14 (14-14) |
| reuse-evidence | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 9,318 (9,314-9,402) | 11,162 (10,961-11,413) | 16 (16-33) |
| reuse-evidence | 2 | reuse | 3 | 3/3 | 3/3 | 0 (0-0) | 1 (1-1) | 4,850 (4,818-4,851) | 6,778 (6,460-6,810) | 15 (13-16) |

## Compared with baseline `candidate` (code `7ab8f32`)

Medians; matched scenario turns only.

| scenario | turn | input tokens before -> after | total tokens before -> after | requests before -> after | queries before -> after | active s before -> after |
| --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | 13,915 -> 9,049 (-35%) | 16,043 -> 10,404 (-35%) | 3 -> 2 (-33%) | 1 -> 1 (+0%) | 23 -> 14 (-40%) |
| reuse-evidence | 1 | 18,681 -> 9,318 (-50%) | 21,239 -> 11,162 (-47%) | 4 -> 2 (-50%) | 3 -> 1 (-67%) | 27 -> 16 (-41%) |
| reuse-evidence | 2 | 4,505 -> 4,850 (+8%) | 5,884 -> 6,778 (+15%) | 1 -> 1 (+0%) | 0 -> 0 | 12 -> 15 (+27%) |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | 1 | `run_5326d9d523c270dd544100c8b401d633` | completed | 1/0/0 | 2 (0, 0) | 9,185/1,410 | 14.0 | 14.0 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-ordinary | 2 | 1 | `run_1b7c0ca305a25ab8da4c1788e066e7ce` | completed | 1/0/0 | 2 (0, 0) | 9,049/1,315 | 13.5 | 13.5 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-ordinary | 3 | 1 | `run_a9f297fb88779d2bfbfba37957fc40c8` | completed | 1/0/0 | 2 (0, 0) | 9,030/1,374 | 13.6 | 13.6 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| reuse-evidence | 1 | 1 | `run_80b3c6d04bee03e0da41712340233504` | completed | 1/0/0 | 2 (0, 0) | 9,318/1,844 | 15.8 | 15.8 | 0 | 3/3 ev, 3/3 text | met | execute_analysis |
| reuse-evidence | 1 | 2 | `run_3d0bb04bd0ea4a40c6a24c78f2964188` | completed | 0/0/0 | 1 (0, 0) | 4,850/1,928 | 15.5 | 15.5 | 0 | 1/2 ev, 2/2 text | met |  |
| reuse-evidence | 2 | 1 | `run_c03e36e6e31d994f2d2e07e99583c862` | completed | 1/0/0 | 2 (0, 0) | 9,402/2,011 | 32.8 | 32.9 | 0 | 3/3 ev, 3/3 text | met | execute_analysis |
| reuse-evidence | 2 | 2 | `run_c28b9515019415d575db8e77cca18fc5` | completed | 0/0/0 | 1 (0, 0) | 4,851/1,959 | 14.9 | 14.9 | 0 | 1/2 ev, 2/2 text | met |  |
| reuse-evidence | 3 | 1 | `run_2aa18326e2c941a3f18e7013a7fff00b` | completed | 1/0/0 | 2 (0, 0) | 9,314/1,647 | 15.6 | 15.6 | 0 | 3/3 ev, 3/3 text | met | execute_analysis |
| reuse-evidence | 3 | 2 | `run_3e71c208d29b9e40d99566b0d27bf65f` | completed | 0/0/0 | 1 (0, 0) | 4,818/1,642 | 12.7 | 12.7 | 0 | 1/2 ev, 2/2 text | met |  |
