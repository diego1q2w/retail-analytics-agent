# Conversation efficiency: t26f9-intent-rescored

- Recorded 2026-10-09T14:59:31+00:00; code `12c5e1c+dirty`; suite `intended-question` v1; scoring v4
- Rescored from `t26f9-intent` (scoring v3) with scoring v4; recorded runs and transcripts unchanged, no model was run again
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 1,500,000 tokens / 300 model attempts; used 372,070 tokens / 40 attempts over 13 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| overview-approved-schema | 1 | cold | 2 | 2/2 | 0/2 | 0 (0-0) | 1 (1-1) | 4,836 (4,836-4,836) | 6,226 (6,124-6,328) | 13 (12-14) |
| spender-after-age-breakdown | 1 | cold | 2 | 1/2 | 2/2 | 2 (1-2) | 2 (2-3) | 15,509 (11,137-19,881) | 19,557 (14,634-24,480) | 33 (30-37) |
| spender-after-age-breakdown | 2 | follow_up | 2 | 2/2 | 2/2 | 0 (0-0) | 1 (1-1) | 6,251 (6,199-6,303) | 9,916 (9,818-10,013) | 22 (22-22) |
| individual-demographics-explicit | 1 | cold | 1 | 0/1 | 0/1 | 1 | 2 | 12,255 | 18,049 | 53 |
| aggregate-demographics-explicit | 1 | cold | 1 | 1/1 | 1/1 | 1 | 2 | 10,985 | 12,971 | 25 |
| monthly-comparison | 1 | investigation | 2 | 2/2 | 2/2 | 4 (4-4) | 7 (7-7) | 62,628 (58,728-66,529) | 73,456 (69,801-77,112) | 97 (87-107) |
| comparison-report-lifecycle | 1 | investigation | 1 | 1/1 | 1/1 | 3 | 4 | 38,847 | 46,692 | 72 |
| comparison-report-lifecycle | 2 | follow_up | 1 | 1/1 | 0/1 | 0 | 6 | 44,593 | 46,900 | 194 |
| comparison-report-lifecycle | 3 | follow_up | 1 | 1/1 | 0/1 | 0 | 3 | 28,105 | 29,148 | 13 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| overview-approved-schema | 1 | 1 | `run_7db1cb66373b37bcdff14b6ad51f4049` | completed | 0/0/0 | 1 (0, 0) | 4,836/1,288 | 12.3 | 12.4 | 0 | - | met |  |
| overview-approved-schema | 2 | 1 | `run_beeb9473cd2ba96cb347f6dd8ac9c6ce` | completed | 0/0/0 | 1 (0, 0) | 4,836/1,492 | 13.8 | 13.8 | 0 | - | met |  |
| spender-after-age-breakdown | 1 | 1 | `run_3634315e164224bce0b14169397cbcee` | completed | 1/0/0 | 2 (0, 0) | 11,137/3,497 | 30.2 | 30.2 | 0 | 2/2 ev, 2/2 text | met | execute_analysis |
| spender-after-age-breakdown | 1 | 2 | `run_d45cbc86feca152b1ace5b0cbf1cdc38` | completed | 0/0/0 | 1 (0, 0) | 6,199/3,814 | 21.7 | 21.8 | 0 | 1/1 ev, 1/1 text | met |  |
| spender-after-age-breakdown | 2 | 1 | `run_d786b0327b081ec7aa01860457d7c8a2` | completed | 2/0/0 | 3 (0, 0) | 19,881/4,599 | 36.6 | 36.6 | 0 | 2/2 ev, 2/2 text | MISSED queries | execute_analysis execute_analysis |
| spender-after-age-breakdown | 2 | 2 | `run_2028d73dd04a64f82179308c615fb2bd` | completed | 0/0/0 | 1 (0, 0) | 6,303/3,515 | 21.8 | 21.8 | 0 | 1/1 ev, 1/1 text | met |  |
| individual-demographics-explicit | 1 | 1 | `run_a72ffb798cdfd523b544b0462e3bd8c4` | partial | 1/0/0 | 2 (0, 0) | 12,255/5,794 | 52.6 | 52.6 | 0 | - | MISSED completed | execute_analysis |
| aggregate-demographics-explicit | 1 | 1 | `run_bfe1368c9bc5800d8baab243beb712a6` | completed | 1/0/0 | 2 (0, 0) | 10,985/1,986 | 25.4 | 25.4 | 0 | 2/2 ev, 2/2 text | met | execute_analysis |
| monthly-comparison | 1 | 1 | `run_7876163aaaea19af209f6c53818cc880` | completed | 4/0/0 | 7 (0, 0) | 66,529/10,583 | 86.9 | 86.9 | 0 | 2/3 ev, 3/3 text | met | load_skill execute_analysis execute_analysis fetch_evidence execute_analysis execute_analysis |
| monthly-comparison | 2 | 1 | `run_7439b52b26a963d87247693120270d82` | completed | 4/0/0 | 7 (0, 0) | 58,728/11,073 | 107.0 | 107.0 | 0 | 2/3 ev, 3/3 text | met | load_skill execute_analysis execute_analysis fetch_evidence execute_analysis execute_analysis |
| comparison-report-lifecycle | 1 | 1 | `run_11186a9bafdecb005d79f7d6787f8c16` | completed | 3/0/0 | 4 (0, 0) | 38,847/7,845 | 72.2 | 72.2 | 0 | 2/2 ev, 2/2 text | met | execute_analysis execute_analysis load_skill execute_analysis fetch_evidence save_report |
| comparison-report-lifecycle | 1 | 2 | `run_d190a8092300258ec3ac51980f975a3f` | completed | 0/0/0 | 6 (1, 0) | 44,593/2,307 | 194.2 | 194.3 | 0 | - | met | load_skill list_reports list_reports read_report |
| comparison-report-lifecycle | 1 | 3 | `run_0c2febfd5d87bb2fe30f7dd9020913af` | completed | 0/0/0 | 3 (0, 0) | 28,105/1,043 | 13.0 | 13.0 | 0 | - | met | load_skill export_report |
