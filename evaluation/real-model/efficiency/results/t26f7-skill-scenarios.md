# Conversation efficiency: t26f7-skill-scenarios

- Recorded 2026-10-09T13:17:56+00:00; code `e32be51`; suite `analytical-skills` v1; scoring v2
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 700,000 tokens / 140 model attempts; used 141,944 tokens / 23 attempts over 7 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| report-lifecycle | 1 | investigation | 1 | 0/1 | 1/1 | 1 | 3 | 18,364 | 21,001 | 25 |
| report-lifecycle | 2 | follow_up | 1 | 1/1 | 1/1 | 0 | 3 | 17,687 | 18,662 | 12 |
| report-lifecycle | 3 | follow_up | 1 | 1/1 | 0/1 | 0 | 3 | 19,022 | 19,704 | 10 |
| preference-correction | 1 | cold | 1 | 1/1 | 1/1 | 1 | 2 | 8,755 | 9,803 | 14 |
| preference-correction | 2 | follow_up | 1 | 1/1 | 1/1 | 0 | 1 | 4,419 | 5,239 | 8 |
| preference-correction | 3 | follow_up | 1 | 1/1 | 0/1 | 0 | 4 | 21,456 | 22,442 | 15 |
| currency-request | 1 | cold | 1 | 0/1 | 1/1 | 1 | 7 | 39,523 | 45,093 | 52 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| report-lifecycle | 1 | 1 | `run_da653e7763e1fcdc782fe965461d9285` | completed | 1/0/0 | 3 (0, 0) | 18,364/2,637 | 24.8 | 24.8 | 0 | 1/1 ev, 1/1 text | MISSED report_with_actions | execute_analysis load_skill save_report |
| report-lifecycle | 1 | 2 | `run_14f0531e501d0428d3ea2f9443fd8d99` | completed | 0/0/0 | 3 (0, 0) | 17,687/975 | 12.4 | 12.4 | 0 | 1/1 ev, 1/1 text | met | load_skill read_report |
| report-lifecycle | 1 | 3 | `run_946987f23a9e6bc7ec0d63c41f1b3a09` | completed | 0/0/0 | 3 (0, 0) | 19,022/682 | 10.1 | 10.1 | 0 | - | met | load_skill export_report |
| preference-correction | 1 | 1 | `run_1a341152fb3adf1e234a0fa46c7b585a` | completed | 1/0/0 | 2 (0, 0) | 8,755/1,048 | 14.2 | 14.2 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| preference-correction | 1 | 2 | `run_b442b5f7fdd5b18f2afca964da6d6fa4` | completed | 0/0/0 | 1 (0, 0) | 4,419/820 | 7.8 | 7.8 | 0 | 1/1 ev, 1/1 text | met |  |
| preference-correction | 1 | 3 | `run_9e6714611d44f5fdea9bb218e2255bd2` | completed | 0/0/0 | 4 (0, 0) | 21,456/986 | 15.4 | 15.4 | 1 | - | met | load_skill remember_preference remember_preference |
| currency-request | 1 | 1 | `run_8b95f2050b1981e9084abcadbf36b988` | partial | 1/0/0 | 7 (0, 0) | 39,523/5,570 | 52.1 | 52.2 | 0 | 1/1 ev, 1/1 text | MISSED completed,no_unexpected_question | execute_analysis load_skill convert_currency convert_currency convert_currency convert_currency |
