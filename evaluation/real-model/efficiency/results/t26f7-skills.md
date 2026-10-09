# Conversation efficiency: t26f7-skills

- Recorded 2026-10-09T13:15:24+00:00; code `e32be51`; suite `conversation-efficiency` v1; scoring v2
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 3,000,000 tokens / 500 model attempts; used 304,504 tokens / 51 attempts over 24 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 8,891 (8,711-9,200) | 10,105 (9,867-11,010) | 14 (14-16) |
| scalar-typo | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 8,914 (8,747-9,173) | 10,158 (9,913-10,736) | 15 (14-16) |
| scalar-explicit | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 8,944 (8,718-9,112) | 10,220 (9,670-10,614) | 13 (13-14) |
| followup-context | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 8,873 (8,748-8,882) | 10,002 (9,914-10,366) | 14 (12-19) |
| followup-context | 2 | follow_up | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 9,402 (9,390-9,873) | 10,609 (10,514-11,358) | 12 (11-14) |
| reuse-evidence | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 9,165 (9,128-9,630) | 11,088 (11,079-11,950) | 16 (16-21) |
| reuse-evidence | 2 | reuse | 3 | 3/3 | 3/3 | 0 (0-0) | 1 (1-1) | 4,698 (4,679-4,768) | 6,353 (6,126-7,272) | 14 (12-19) |
| clarify-missing-month | 1 | clarification | 1 | 1/1 | 1/1 | 1 | 4 | 17,639 | 20,117 | 28 |
| why-category-change | 1 | investigation | 1 | 1/1 | 1/1 | 2 | 4 | 23,705 | 31,035 | 55 |
| report-concentration | 1 | investigation | 1 | 1/1 | 1/1 | 3 | 4 | 34,606 | 44,428 | 71 |

## Compared with baseline `candidate` (code `7ab8f32`)

Medians; matched scenario turns only.

| scenario | turn | input tokens before -> after | total tokens before -> after | requests before -> after | queries before -> after | active s before -> after |
| --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | 13,915 -> 8,891 (-36%) | 16,043 -> 10,105 (-37%) | 3 -> 2 (-33%) | 1 -> 1 (+0%) | 23 -> 14 (-37%) |
| scalar-typo | 1 | 8,375 -> 8,914 (+6%) | 9,603 -> 10,158 (+6%) | 2 -> 2 (+0%) | 1 -> 1 (+0%) | 13 -> 15 (+11%) |
| scalar-explicit | 1 | 8,331 -> 8,944 (+7%) | 9,296 -> 10,220 (+10%) | 2 -> 2 (+0%) | 1 -> 1 (+0%) | 12 -> 13 (+10%) |
| followup-context | 1 | 8,355 -> 8,873 (+6%) | 9,510 -> 10,002 (+5%) | 2 -> 2 (+0%) | 1 -> 1 (+0%) | 12 -> 14 (+15%) |
| followup-context | 2 | 9,010 -> 9,402 (+4%) | 10,065 -> 10,609 (+5%) | 2 -> 2 (+0%) | 1 -> 1 (+0%) | 12 -> 12 (+7%) |
| reuse-evidence | 1 | 18,681 -> 9,165 (-51%) | 21,239 -> 11,088 (-48%) | 4 -> 2 (-50%) | 3 -> 1 (-67%) | 27 -> 16 (-39%) |
| reuse-evidence | 2 | 4,505 -> 4,698 (+4%) | 5,884 -> 6,353 (+8%) | 1 -> 1 (+0%) | 0 -> 0 | 12 -> 14 (+24%) |
| clarify-missing-month | 1 | 12,246 -> 17,639 (+44%) | 14,678 -> 20,117 (+37%) | 3 -> 4 (+33%) | 1 -> 1 (+0%) | 25 -> 28 (+10%) |
| why-category-change | 1 | 25,227 -> 23,705 (-6%) | 33,118 -> 31,035 (-6%) | 4 -> 4 (+0%) | 3 -> 2 (-33%) | 64 -> 55 (-14%) |
| report-concentration | 1 | 46,258 -> 34,606 (-25%) | 54,709 -> 44,428 (-19%) | 6 -> 4 (-33%) | 4 -> 3 (-25%) | 70 -> 71 (+2%) |

## Every repetition

| scenario | rep | turn | run | status | queries ok/rejected/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | 1 | `run_2e1881693ab912f909c1a0d1ec4f852f` | completed | 1/0/0 | 2 (0, 0) | 8,711/1,156 | 14.3 | 14.4 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-ordinary | 2 | 1 | `run_87c8e545c25fc79883ae24d3565b290d` | completed | 1/0/0 | 2 (0, 0) | 8,891/1,214 | 14.1 | 14.1 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-ordinary | 3 | 1 | `run_01b1185fbbe3ad8779ebcdb5956158ad` | completed | 1/0/0 | 2 (0, 0) | 9,200/1,810 | 16.1 | 16.1 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-typo | 1 | 1 | `run_c150469524a1fbd5c7cc9d076c87c3a3` | completed | 1/0/0 | 2 (0, 0) | 9,173/1,563 | 15.5 | 15.5 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-typo | 2 | 1 | `run_73cfb1732cca326965f644d6e24274c2` | completed | 1/0/0 | 2 (0, 0) | 8,914/1,244 | 14.6 | 14.6 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-typo | 3 | 1 | `run_b2dcb295a71833f77c85395549562c2d` | completed | 1/0/0 | 2 (0, 0) | 8,747/1,166 | 13.6 | 13.6 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-explicit | 1 | 1 | `run_be5cdc9043a22b780e6a0924b55bd147` | completed | 1/0/0 | 2 (0, 0) | 9,112/1,502 | 13.7 | 13.7 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-explicit | 2 | 1 | `run_7c5a3100e55f919623a9559774b752ab` | completed | 1/0/0 | 2 (0, 0) | 8,718/952 | 13.0 | 13.0 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-explicit | 3 | 1 | `run_fe483ad57347aa75a1c790c3773d7c36` | completed | 1/0/0 | 2 (0, 0) | 8,944/1,276 | 12.9 | 12.9 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 1 | 1 | `run_ca6b81d607135d8341dbe0919ab055b5` | completed | 1/0/0 | 2 (0, 0) | 8,748/1,166 | 12.4 | 12.4 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 1 | 2 | `run_eb06aabc1a45b0e66aa3de8d1a5ee8d3` | completed | 1/0/0 | 2 (0, 0) | 9,873/1,485 | 14.4 | 14.4 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 2 | 1 | `run_ee08ab29ac1468f904d690298aa0c496` | completed | 1/0/0 | 2 (0, 0) | 8,882/1,120 | 19.2 | 19.2 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 2 | 2 | `run_cc8033ef07a4e6b0afb7cf10d5502315` | completed | 1/0/0 | 2 (0, 0) | 9,390/1,124 | 11.0 | 11.0 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 3 | 1 | `run_820ad629a1d326cc5160dde846448d14` | completed | 1/0/0 | 2 (0, 0) | 8,873/1,493 | 14.1 | 14.1 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 3 | 2 | `run_0a6ca2fe9de7dd2b748c424bb385da88` | completed | 1/0/0 | 2 (0, 0) | 9,402/1,207 | 12.3 | 12.3 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| reuse-evidence | 1 | 1 | `run_29efa32f647c12e261b0c07abfde2e0d` | completed | 1/0/0 | 2 (0, 0) | 9,630/2,320 | 20.6 | 20.6 | 0 | 3/3 ev, 3/3 text | met | execute_analysis |
| reuse-evidence | 1 | 2 | `run_ad2a6cfe4806545548259fdc3acd6f76` | completed | 0/0/0 | 1 (0, 0) | 4,768/2,504 | 19.3 | 19.3 | 0 | 1/2 ev, 2/2 text | met |  |
| reuse-evidence | 2 | 1 | `run_b763f0ddd31400bcf48e59a88a1292c0` | completed | 1/0/0 | 2 (0, 0) | 9,128/1,960 | 16.1 | 16.1 | 0 | 3/3 ev, 3/3 text | met | execute_analysis |
| reuse-evidence | 2 | 2 | `run_6640b1105acb87e3f0ad559ecc0efc79` | completed | 0/0/0 | 1 (0, 0) | 4,698/1,428 | 11.7 | 11.7 | 0 | 1/2 ev, 2/2 text | met |  |
| reuse-evidence | 3 | 1 | `run_6b98fc04bb0d6c9b117e70e327ef6541` | completed | 1/0/0 | 2 (0, 0) | 9,165/1,914 | 16.3 | 16.3 | 0 | 3/3 ev, 3/3 text | met | execute_analysis |
| reuse-evidence | 3 | 2 | `run_29a149acbb9b4e4a38953164d9838da0` | completed | 0/0/0 | 1 (0, 0) | 4,679/1,674 | 14.5 | 14.5 | 0 | 1/2 ev, 2/2 text | met |  |
| clarify-missing-month | 1 | 1 | `run_8f65e383b8bbef7c5892e26fc2b6270c` | completed | 1/0/0 | 4 (0, 0) | 17,639/2,478 | 27.5 | 27.7 | 0 | 1/1 ev, 1/1 text | met | fetch_evidence execute_analysis |
| why-category-change | 1 | 1 | `run_3332eed3d319400818dad56a54026825` | completed | 2/0/0 | 4 (0, 0) | 23,705/7,330 | 54.7 | 54.7 | 0 | 6/7 ev, 7/7 text | met | load_skill execute_analysis execute_analysis |
| report-concentration | 1 | 1 | `run_b9c8e0d9a7a2761015e7de047af72b3b` | completed | 3/0/0 | 4 (0, 0) | 34,606/9,822 | 71.1 | 71.1 | 0 | 3/3 ev, 3/3 text | met | execute_analysis load_skill execute_analysis execute_analysis save_report |
