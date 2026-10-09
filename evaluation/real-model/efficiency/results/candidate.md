# Conversation efficiency: candidate

- Recorded 2026-10-09T12:16:05+00:00; code `7ab8f32`; suite `conversation-efficiency` v1; scoring v2
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 3,000,000 tokens / 500 model attempts; used 360,876 tokens / 63 attempts over 24 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-2) | 3 (3-4) | 13,915 (13,318-19,551) | 16,043 (15,537-23,720) | 23 (21-39) |
| scalar-typo | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 8,375 (8,363-8,394) | 9,603 (9,402-9,766) | 13 (13-15) |
| scalar-explicit | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 8,331 (8,311-8,449) | 9,296 (9,265-9,395) | 12 (11-12) |
| followup-context | 1 | cold | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 8,355 (8,343-8,554) | 9,510 (9,471-9,818) | 12 (11-14) |
| followup-context | 2 | follow_up | 3 | 3/3 | 3/3 | 1 (1-1) | 2 (2-2) | 9,010 (8,921-9,088) | 10,065 (9,974-10,215) | 12 (10-12) |
| reuse-evidence | 1 | cold | 3 | 0/3 | 3/3 | 3 (2-4) | 4 (3-6) | 18,681 (13,455-28,684) | 21,239 (15,755-32,300) | 27 (26-42) |
| reuse-evidence | 2 | reuse | 3 | 3/3 | 3/3 | 0 (0-0) | 1 (1-1) | 4,505 (4,409-4,588) | 5,884 (5,733-6,380) | 12 (11-14) |
| clarify-missing-month | 1 | clarification | 1 | 1/1 | 1/1 | 1 | 3 | 12,246 | 14,678 | 25 |
| why-category-change | 1 | investigation | 1 | 1/1 | 1/1 | 3 | 4 | 25,227 | 33,118 | 64 |
| report-concentration | 1 | investigation | 1 | 1/1 | 1/1 | 4 | 6 | 46,258 | 54,709 | 70 |

## Compared with baseline `baseline` (code `12373a0`)

Medians; matched scenario turns only.

| scenario | turn | input tokens before -> after | total tokens before -> after | requests before -> after | queries before -> after | active s before -> after |
| --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | 50,593 -> 13,915 (-72%) | 55,460 -> 16,043 (-71%) | 9 -> 3 (-67%) | 4 -> 1 (-75%) | 54 -> 23 (-58%) |
| scalar-typo | 1 | 0 -> 8,375 | 0 -> 9,603 | 0 -> 2 | 0 -> 1 | 0 -> 13 |
| scalar-explicit | 1 | 35,009 -> 8,331 (-76%) | 37,256 -> 9,296 (-75%) | 7 -> 2 (-71%) | 1 -> 1 (+0%) | 30 -> 12 (-60%) |
| followup-context | 1 | 40,672 -> 8,355 (-79%) | 43,701 -> 9,510 (-78%) | 8 -> 2 (-75%) | 2 -> 1 (-50%) | 44 -> 12 (-72%) |
| followup-context | 2 | 38,615 -> 9,010 (-77%) | 43,157 -> 10,065 (-77%) | 6 -> 2 (-67%) | 2 -> 1 (-50%) | 39 -> 12 (-71%) |
| reuse-evidence | 1 | 60,405 -> 18,681 (-69%) | 65,659 -> 21,239 (-68%) | 11 -> 4 (-64%) | 5 -> 3 (-40%) | 66 -> 27 (-60%) |
| reuse-evidence | 2 | 5,716 -> 4,505 (-21%) | 7,724 -> 5,884 (-24%) | 1 -> 1 (+0%) | 0 -> 0 | 16 -> 12 (-29%) |
| clarify-missing-month | 1 | 80,096 -> 12,246 (-85%) | 85,683 -> 14,678 (-83%) | 15 -> 3 (-80%) | 3 -> 1 (-67%) | 70 -> 25 (-65%) |
| why-category-change | 1 | 81,172 -> 25,227 (-69%) | 89,653 -> 33,118 (-63%) | 11 -> 4 (-64%) | 6 -> 3 (-50%) | 83 -> 64 (-23%) |
| report-concentration | 1 | 75,240 -> 46,258 (-39%) | 85,882 -> 54,709 (-36%) | 10 -> 6 (-40%) | 5 -> 4 (-20%) | 96 -> 70 (-28%) |

## Every repetition

| scenario | rep | turn | run | status | queries ok/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | 1 | `run_7684c6c4a85a8e633f7f72f886b1e298` | completed | 1/1 | 3 (0, 0) | 13,915/2,128 | 21.1 | 21.2 | 0 | 1/1 ev, 1/1 text | met | execute_analysis execute_analysis |
| scalar-ordinary | 2 | 1 | `run_29693f140af5be83656f019cada5bc7c` | completed | 1/1 | 3 (0, 0) | 13,318/2,219 | 22.8 | 22.8 | 0 | 1/1 ev, 1/1 text | met | execute_analysis execute_analysis |
| scalar-ordinary | 3 | 1 | `run_6eaae4e4e595e1b4d71ef3b7491c9bdc` | completed | 2/1 | 4 (0, 0) | 19,551/4,169 | 38.7 | 38.7 | 0 | 1/1 ev, 1/1 text | met | execute_analysis execute_analysis execute_analysis |
| scalar-typo | 1 | 1 | `run_1b2bba34b80c9462fb1fb943a1ceb110` | completed | 1/0 | 2 (0, 0) | 8,394/1,209 | 12.6 | 12.6 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-typo | 2 | 1 | `run_cfab59e517691003d802ab695213adc3` | completed | 1/0 | 2 (0, 0) | 8,363/1,039 | 13.2 | 13.2 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-typo | 3 | 1 | `run_d7f0920b07297db4fbfe8df6167b7033` | completed | 1/0 | 2 (0, 0) | 8,375/1,391 | 14.6 | 14.6 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-explicit | 1 | 1 | `run_7e01b893f7623c145360ab165b59b87e` | completed | 1/0 | 2 (0, 0) | 8,449/946 | 11.8 | 11.8 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-explicit | 2 | 1 | `run_e682f1f412e60eb428b9264bff21b934` | completed | 1/0 | 2 (0, 0) | 8,311/954 | 10.8 | 10.8 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| scalar-explicit | 3 | 1 | `run_d22cb389096a98ffe7fcf4ff36f53dca` | completed | 1/0 | 2 (0, 0) | 8,331/965 | 11.8 | 11.8 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 1 | 1 | `run_881ad39fdbf3fa1a70a5e3f29ccd404b` | completed | 1/0 | 2 (0, 0) | 8,343/1,167 | 12.3 | 12.3 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 1 | 2 | `run_c57e407a80753eaf10d4599907fef98a` | completed | 1/0 | 2 (0, 0) | 8,921/1,053 | 10.4 | 10.5 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 2 | 1 | `run_0902cd770da979939579ae2cc3fe938c` | completed | 1/0 | 2 (0, 0) | 8,355/1,116 | 11.1 | 11.1 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 2 | 2 | `run_b25943c94f2aadac80120a608c6ce81c` | completed | 1/0 | 2 (0, 0) | 9,088/1,127 | 11.9 | 11.9 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 3 | 1 | `run_3ec4aac970d84b969e9779355bbe793c` | completed | 1/0 | 2 (0, 0) | 8,554/1,264 | 14.0 | 14.0 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| followup-context | 3 | 2 | `run_38cdc86af479efc46c629b9831f832a6` | completed | 1/0 | 2 (0, 0) | 9,010/1,055 | 11.5 | 11.5 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| reuse-evidence | 1 | 1 | `run_cb6a202f3310b1d8f106390795011680` | completed | 2/0 | 3 (0, 0) | 13,455/2,300 | 25.5 | 25.5 | 0 | 3/3 ev, 3/3 text | MISSED queries | execute_analysis execute_analysis |
| reuse-evidence | 1 | 2 | `run_71774aef84ab2bbbe7f45a79bda009bb` | completed | 0/0 | 1 (0, 0) | 4,505/1,379 | 11.7 | 11.7 | 0 | 1/2 ev, 2/2 text | met |  |
| reuse-evidence | 2 | 1 | `run_a90bd266f89a7496efb08b73a6ee29eb` | completed | 4/0 | 6 (0, 0) | 28,684/3,616 | 42.4 | 42.4 | 0 | 3/3 ev, 3/3 text | MISSED queries,model_requests | execute_analysis find_analysis_examples execute_analysis execute_analysis execute_analysis |
| reuse-evidence | 2 | 2 | `run_a061e3fe65cb58f437ffcb58b18a8b8b` | completed | 0/0 | 1 (0, 0) | 4,409/1,324 | 11.2 | 11.2 | 0 | 1/2 ev, 2/2 text | met |  |
| reuse-evidence | 3 | 1 | `run_f99596b098712a0fb6319dcf26c23be8` | completed | 3/0 | 4 (0, 0) | 18,681/2,558 | 26.6 | 26.7 | 0 | 3/3 ev, 3/3 text | MISSED queries | execute_analysis execute_analysis execute_analysis |
| reuse-evidence | 3 | 2 | `run_a12817383ad72afc330ce3d525a26154` | completed | 0/0 | 1 (0, 0) | 4,588/1,792 | 13.9 | 13.9 | 0 | 1/2 ev, 2/2 text | met |  |
| clarify-missing-month | 1 | 1 | `run_359f6f6335ebe94c42a276c6491d48fd` | completed | 1/0 | 3 (0, 0) | 12,246/2,432 | 25.0 | 25.1 | 0 | 1/1 ev, 1/1 text | met | execute_analysis |
| why-category-change | 1 | 1 | `run_95f4101b1f8019525455e98f4f7fcf1d` | completed | 3/0 | 4 (0, 0) | 25,227/7,891 | 63.6 | 63.6 | 0 | 6/7 ev, 7/7 text | met | execute_analysis execute_analysis execute_analysis |
| report-concentration | 1 | 1 | `run_65535c59be361cef2d5a5503122a8b1f` | completed | 4/0 | 6 (0, 0) | 46,258/8,451 | 69.7 | 69.7 | 0 | 3/3 ev, 3/3 text | met | execute_analysis execute_analysis execute_analysis execute_analysis save_report |
