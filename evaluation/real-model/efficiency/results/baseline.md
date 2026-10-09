# Conversation efficiency: baseline

- Recorded 2026-10-09T11:31:14+00:00; code `12373a0`; suite `conversation-efficiency` v1; scoring v2
- Target `agent_runtime:local` (backend `local`), warehouse `offline DuckDB (frozen extract)` (`thelook-realdata-extract-1`, digest `8ba6e7f40602`)
- Configured models: google-interactions=gemini-3.8-flash, openai=gpt-5-mini
- Providers that answered: google-interactions:gemini-3.8-flash
- Spend ceiling: 3,000,000 tokens / 500 model attempts; used 1,013,048 tokens / 159 attempts over 24 runs

## Aggregates per turn (median (min-max) over repetitions)

| scenario | turn | kind | n | targets met | figures right | queries | model requests | input tokens | total tokens | active s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | cold | 3 | 0/3 | 3/3 | 4 (4-5) | 9 (7-11) | 50,593 (36,956-67,570) | 55,460 (40,289-72,640) | 54 (38-62) |
| scalar-typo | 1 | cold | 3 | 0/3 | 0/3 | 0 (0-0) | 0 (0-0) | 0 (0-0) | 0 (0-0) | 0 (0-0) |
| scalar-explicit | 1 | cold | 3 | 0/3 | 3/3 | 1 (1-1) | 7 (6-7) | 35,009 (29,288-36,053) | 37,256 (31,192-37,740) | 30 (28-34) |
| followup-context | 1 | cold | 3 | 0/3 | 3/3 | 2 (2-2) | 8 (7-8) | 40,672 (35,748-41,875) | 43,701 (39,231-44,682) | 44 (39-58) |
| followup-context | 2 | follow_up | 3 | 0/3 | 3/3 | 2 (2-2) | 6 (6-6) | 38,615 (38,477-40,749) | 43,157 (42,392-44,783) | 39 (39-45) |
| reuse-evidence | 1 | cold | 3 | 0/3 | 3/3 | 5 (5-5) | 11 (10-11) | 60,405 (58,176-60,674) | 65,659 (64,182-66,078) | 66 (64-71) |
| reuse-evidence | 2 | reuse | 3 | 3/3 | 3/3 | 0 (0-0) | 1 (1-1) | 5,716 (5,693-5,963) | 7,724 (7,373-8,291) | 16 (12-18) |
| clarify-missing-month | 1 | clarification | 1 | 0/1 | 1/1 | 3 | 15 | 80,096 | 85,683 | 70 |
| why-category-change | 1 | investigation | 1 | 0/1 | 0/1 | 6 | 11 | 81,172 | 89,653 | 83 |
| report-concentration | 1 | investigation | 1 | 1/1 | 1/1 | 5 | 10 | 75,240 | 85,882 | 96 |

## Every repetition

| scenario | rep | turn | run | status | queries ok/failed | requests (failed, fallback) | tokens in/out | active s | wall s | restarts | figures | targets | tools |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| scalar-ordinary | 1 | 1 | `run_842904c7913fe3bc7c8bdef9f8bb6dcf` | completed | 4/0 | 9 (0, 0) | 50,593/4,867 | 54.1 | 54.1 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | inspect_preferences list_relations describe_relation describe_relation find_analysis_examples describe_relation execute_analysis execute_analysis execute_analysis execute_analysis |
| scalar-ordinary | 2 | 1 | `run_05f59e460417713edeb19a392215e6d1` | completed | 4/0 | 7 (0, 0) | 36,956/3,333 | 38.2 | 38.2 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | list_relations describe_relation describe_relation execute_analysis execute_analysis execute_analysis execute_analysis |
| scalar-ordinary | 3 | 1 | `run_3c88518dd0a894342934983338c14d6d` | completed | 5/0 | 11 (0, 0) | 67,570/5,070 | 61.8 | 61.8 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | list_relations describe_relation describe_relation find_analysis_examples execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis inspect_preferences |
| scalar-typo | 1 | 1 | `run_be9771ea445eb46b09a35e996cfa5d0d` | cancelled | 0/0 | 0 (0, 0) | 0/0 | 0.0 | 0.2 | 0 | 0/1 ev, 0/1 text | MISSED completed,no_unexpected_question |  |
| scalar-typo | 2 | 1 | `run_ce905b59cbb652e72ad692de92a2322f` | cancelled | 0/0 | 0 (0, 0) | 0/0 | 0.1 | 0.2 | 0 | 0/1 ev, 0/1 text | MISSED completed,no_unexpected_question |  |
| scalar-typo | 3 | 1 | `run_b0f4354b7b796ca622465d8d734df2e5` | cancelled | 0/0 | 0 (0, 0) | 0/0 | 0.0 | 0.2 | 0 | 0/1 ev, 0/1 text | MISSED completed,no_unexpected_question |  |
| scalar-explicit | 1 | 1 | `run_c783f22346ccb7238fea5823052e717f` | completed | 1/0 | 6 (0, 0) | 29,288/1,904 | 27.5 | 27.5 | 0 | 1/1 ev, 1/1 text | MISSED model_requests | list_relations describe_relation describe_relation inspect_preferences find_analysis_examples execute_analysis |
| scalar-explicit | 2 | 1 | `run_2393ecfe1825f88a72153cdd28e95a38` | completed | 1/0 | 7 (0, 0) | 36,053/1,687 | 29.7 | 29.7 | 0 | 1/1 ev, 1/1 text | MISSED model_requests | list_relations describe_relation describe_relation find_analysis_examples inspect_preferences execute_analysis describe_relation |
| scalar-explicit | 3 | 1 | `run_2e0499c8110e09b736c2eece469da3dd` | completed | 1/0 | 7 (0, 0) | 35,009/2,247 | 34.4 | 34.4 | 0 | 1/1 ev, 1/1 text | MISSED model_requests | list_relations describe_relation describe_relation find_analysis_examples inspect_preferences describe_relation execute_analysis |
| followup-context | 1 | 1 | `run_e4ea7d3f39ab58383ccdfc16e3490562` | completed | 2/0 | 8 (0, 0) | 41,875/2,807 | 39.0 | 39.0 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | list_relations describe_relation describe_relation inspect_preferences find_analysis_examples execute_analysis execute_analysis describe_relation |
| followup-context | 1 | 2 | `run_6bce4c9e4f1e5ba7cb0a4579f18d9763` | completed | 2/0 | 6 (0, 0) | 38,615/4,542 | 44.7 | 44.7 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | list_relations describe_relation inspect_preferences execute_analysis execute_analysis |
| followup-context | 2 | 1 | `run_425ab0373ede5eb5a6eef550a75a82ac` | completed | 2/0 | 7 (0, 0) | 35,748/3,483 | 57.5 | 57.5 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | list_relations describe_relation describe_relation find_analysis_examples inspect_preferences describe_relation execute_analysis execute_analysis |
| followup-context | 2 | 2 | `run_5345d44168d246297ea1ca13941f9724` | completed | 2/0 | 6 (0, 0) | 40,749/4,034 | 39.3 | 39.3 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | list_relations describe_relation describe_relation execute_analysis execute_analysis inspect_preferences |
| followup-context | 3 | 1 | `run_3b666d5aeb9d7e019c3e8a10422caa8f` | completed | 2/0 | 8 (0, 0) | 40,672/3,029 | 43.7 | 43.7 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | list_relations describe_relation describe_relation inspect_preferences find_analysis_examples execute_analysis execute_analysis |
| followup-context | 3 | 2 | `run_5616813f32365085a4567813b6341b47` | completed | 2/0 | 6 (0, 0) | 38,477/3,915 | 38.9 | 38.9 | 0 | 1/1 ev, 1/1 text | MISSED queries,model_requests | list_relations describe_relation execute_analysis execute_analysis inspect_preferences |
| reuse-evidence | 1 | 1 | `run_d0696cb834a2df0b61eeea41c1efdb19` | completed | 5/0 | 11 (0, 0) | 60,405/5,254 | 63.8 | 63.8 | 0 | 3/3 ev, 3/3 text | MISSED queries,model_requests | list_relations describe_relation describe_relation inspect_preferences find_analysis_examples execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis |
| reuse-evidence | 1 | 2 | `run_f18e229ebf422b8c9b112d902c5b66f8` | completed | 0/0 | 1 (0, 0) | 5,963/2,328 | 17.9 | 17.9 | 0 | 1/2 ev, 2/2 text | met |  |
| reuse-evidence | 2 | 1 | `run_9ca3e8f6513c92490956678421fd81f4` | completed | 5/0 | 11 (0, 0) | 60,674/5,404 | 65.7 | 65.7 | 0 | 3/3 ev, 3/3 text | MISSED queries,model_requests | list_relations describe_relation describe_relation inspect_preferences find_analysis_examples execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis |
| reuse-evidence | 2 | 2 | `run_5b9c102b0548338e025fa5bb1fc86987` | completed | 0/0 | 1 (0, 0) | 5,716/2,008 | 16.4 | 16.4 | 0 | 1/2 ev, 2/2 text | met |  |
| reuse-evidence | 3 | 1 | `run_866eaf19529bf0d830a3c3e8b0702ff6` | completed | 5/0 | 10 (0, 0) | 58,176/6,006 | 71.4 | 71.4 | 0 | 3/3 ev, 3/3 text | MISSED queries,model_requests | list_relations describe_relation describe_relation find_analysis_examples inspect_preferences execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis |
| reuse-evidence | 3 | 2 | `run_be6ea54a458f93530e553552fda05beb` | completed | 0/0 | 1 (0, 0) | 5,693/1,680 | 11.5 | 11.5 | 0 | 1/2 ev, 2/2 text | met |  |
| clarify-missing-month | 1 | 1 | `run_b623deb2b1b8a3eb2b3b9f0bd9f505ba` | completed | 3/0 | 15 (0, 0) | 80,096/5,587 | 70.5 | 70.7 | 0 | 1/1 ev, 1/1 text | MISSED queries | list_reports fetch_evidence inspect_preferences list_relations list_relations describe_relation describe_relation find_analysis_examples describe_relation inspect_preferences execute_analysis execute_analysis execute_analysis fetch_evidence |
| why-category-change | 1 | 1 | `run_b4530d5dc4bf41535c8f3d4268c35177` | partial | 6/1 | 11 (1, 0) | 81,172/8,481 | 82.7 | 82.7 | 0 | 6/7 ev, 2/7 text | MISSED completed | list_relations describe_relation describe_relation describe_relation find_analysis_examples execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis |
| report-concentration | 1 | 1 | `run_b2ee5ba1d21f9d79c04df471fc908314` | completed | 5/1 | 10 (0, 0) | 75,240/10,642 | 96.4 | 96.4 | 0 | 3/3 ev, 3/3 text | met | inspect_preferences list_relations describe_relation describe_relation describe_relation find_analysis_examples execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis execute_analysis save_report |
