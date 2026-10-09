# Security verification record

This record covers the mandatory privacy, product-authorization,
malicious-input and deletion-confirmation controls, and the four additional
release gates for revoked evidence, release-time recheck, names and
analysis-only scope. The checks reuse the existing suites. New tests were
added only where a core boundary had no direct case. Every restriction below
is paired with a positive control (a legitimate request that must still
succeed), so a check that always refuses does not pass.

It is a focused verification, not a privacy certification. Required controls
are reported one by one. They are never averaged into a quality score.

## Result

| Run | Backend | Result |
| --- | --- | --- |
| Unit selection (in-memory fakes, DuckDB privacy oracle) | none (backend-neutral application code) | 896 passed |
| Docker selection, PostgreSQL only | local (default) | 46 passed |
| Docker selection, PostgreSQL + Temporal | temporal | 5 passed |

There were no skips. An `xfail(strict=True)` case is an open release blocker,
not a pass. When the fix lands, the case starts passing, and strict mode then
fails the run until the marker is removed.

**All four release gates are met.** G-1 (release-time recheck of uncited
conclusions) and G-2 (names in Golden knowledge text) are fixed (see
[Open findings](#open-findings)).

## Commands

Run from the repository root with the project virtualenv active:

```sh
# Unit selection (backend-neutral)
python -m pytest tests/unit/security tests/unit/privacy tests/unit/context \
  tests/unit/test_authorization.py tests/unit/test_guarded_model_history.py \
  tests/unit/reports tests/unit/sql_compiler/test_attacks.py \
  tests/unit/http/test_api.py tests/unit/http/test_conversations.py \
  tests/unit/tools/test_gateway.py tests/unit/tools/test_agent_capabilities.py \
  tests/unit/cli/test_commands.py tests/unit/persona/test_screening.py \
  tests/unit/test_knowledge.py tests/unit/evidence/test_report_reuse.py \
  tests/unit/test_retrieval.py tests/unit/test_sensitive_content.py -q

# Docker, local backend (PostgreSQL only)
python -m pytest -m docker tests/integration/test_security_release_gates.py \
  tests/integration/test_context.py tests/integration/test_access.py \
  tests/integration/test_access_audit.py tests/integration/test_report_deletion.py \
  tests/integration/test_report_scope_coverage.py tests/integration/test_reports.py \
  tests/integration/test_report_reuse_withdrawal.py \
  tests/integration/test_local_http_api.py -q

# Docker, Temporal backend
python -m pytest -m docker \
  "tests/integration/test_investigations.py::test_fresh_authority_at_model_tool_and_release_boundaries" \
  "tests/integration/test_investigations.py::test_release_masks_email_and_withholds_revoked_evidence" \
  tests/integration/test_http_api.py -q
```

Docker tests create a throwaway Compose project per module, with free
loopback ports, and remove only that project afterwards. Setting
`RA_TEST_PROJECT_TAG` labels the project names.

## Control matrix

Case IDs are test node IDs. Paths are relative to `tests/`.

### PII masking versus permitted demographics

| Control | Adversarial cases | Positive control |
| --- | --- | --- |
| No direct identifiers or exact ages in released rows | `unit/privacy/test_customer_privacy.py::test_exact_age_identifiers_and_raw_keys_are_rejected`, `::test_no_query_resolves_age_below_the_grid`, `::test_user_supplied_raw_id_does_not_find_a_customer`; `unit/privacy/test_result_boundary.py::test_age_band_column_with_an_exact_or_off_grid_age_is_withheld`, `::test_reference_column_with_a_non_reference_is_withheld` | `test_customer_privacy.py::test_individual_customer_detail_includes_demographics`, `::test_tiny_demographic_groups_are_released_without_suppression` (no minimum cohort, by policy) |
| Opaque references are per-executive and keyed | `test_customer_privacy.py::test_references_cannot_be_joined_across_executives`, `::test_wrong_executive_reference_matches_nothing`, `::test_references_fail_closed_without_a_key` | `::test_drill_down_by_reference_returns_permitted_history` |
| Generated text: PII masked for display, blocks report/memory | `unit/context/test_output_gate.py::test_pii_smuggled_in_model_text_is_masked_for_display_and_blocks_reports` (11 encodings), `::test_all_or_nothing_release`, `::test_detector_failure_withholds_the_section` | `::test_positive_demographic_answer_with_permitted_reference_is_released` |
| PII never enters model context | `unit/context/test_context_builder.py::test_released_rows_never_carry_source_pii_into_context`, `::test_user_supplied_personal_data_is_masked_before_the_model`; `unit/context/test_fetch_evidence.py::test_pages_never_carry_personal_data` | `unit/context/test_disclosure.py::test_demographics_figures_and_references_are_not_personal_data` |

### Product authorization (per executive)

| Control | Adversarial cases | Positive control |
| --- | --- | --- |
| Compiler scope and forbidden data | `unit/sql_compiler/test_attacks.py` (forbidden data, unsupported SQL, policy-parameter tampering); `unit/security/test_release_gates.py::test_g4_admitted_injection_still_cannot_cross_the_product_boundary` | same test: the query returns product 2 only for a scope that holds it |
| Cross-owner records look missing | `unit/test_authorization.py::test_other_executives_records_look_exactly_like_missing_ones`; `integration/test_access.py::test_executives_cannot_reach_each_others_sessions_runs_or_operations`; `unit/context/test_context_builder.py::test_other_executives_runs_and_evidence_are_unreachable` | `unit/test_authorization.py::test_authentication_maps_a_verified_subject_to_an_active_executive` |
| Entitlement changes apply on the next check | `unit/test_authorization.py::test_entitlement_changes_are_visible_to_the_next_check`; `integration/test_access.py::test_entitlement_updates_bump_the_version_seen_by_fresh_checks`; `unit/privacy/test_result_boundary.py::test_revoked_access_withholds_computed_rows` | `unit/privacy/test_result_boundary.py::test_valid_rows_are_released_with_roles` |
| Reports follow their required scope | `unit/reports/test_report_scope_coverage.py::test_removing_a_required_product_blocks`, `::test_the_model_cannot_supply_a_scope`; `integration/test_report_scope_coverage.py` | `::test_widening_keeps_old_reports_readable`, `::test_removing_an_unrelated_product_keeps_access` |

### Malicious input: identity, prompt and tool injection

| Control | Adversarial cases | Positive control |
| --- | --- | --- |
| Identity comes only from the verified token | `unit/http/test_api.py::test_body_cannot_carry_identity_or_unknown_fields`, `::test_token_in_query_string_is_not_accepted`, `::test_every_route_requires_a_valid_bearer_token`; `unit/test_authorization.py::test_context_comes_from_server_state_and_token_scopes_only_narrow`; `integration/test_http_api.py::test_authentication_failures` | `integration/test_http_api.py::test_run_lifecycle_replay_after_disconnect_and_two_users` |
| Untrusted text stays data | `unit/context/test_context_builder.py::test_untrusted_text_cannot_break_out_of_its_block`; `unit/tools/test_gateway.py::test_malicious_extra_arguments_are_rejected`, `::test_hidden_and_unknown_tools_look_the_same` | `unit/tools/test_gateway.py::test_sql_success_is_typed_and_correlated` |
| Secrets never released | `unit/context/test_output_gate.py::test_secrets_and_memory_references_fail_closed` | same test: a permitted reference is displayed |

### Human-only deletion confirmation

| Control | Adversarial cases | Positive control |
| --- | --- | --- |
| Only the user confirms, by exact proposal | `unit/reports/test_report_deletion.py::test_capability_proposes_but_cannot_confirm`, `::test_no_confirmation_means_no_deletion`, `::test_another_principal_cannot_confirm_or_see_the_proposal`; `unit/http/test_api.py::test_deletion_confirmation_needs_explicit_approval`; `unit/cli/test_commands.py::test_deletion_needs_the_exact_typed_phrase_and_shows_every_report`, `::test_deletion_has_no_blanket_yes_flag` | `unit/reports/test_report_deletion.py::test_confirmation_deletes_exactly_the_proposed_reports` |
| Stale, expired and replayed confirmations | `unit/reports/test_report_deletion.py::test_expired_proposal_deletes_nothing`, `::test_replay_fails_and_deletes_once`, `::test_new_version_makes_the_proposal_stale`; `integration/test_report_deletion.py::test_expiry_wrong_owner_and_stale_version_delete_nothing`, `::test_confirmation_racing_a_new_version_never_deletes_unconfirmed_content` | `integration/test_report_deletion.py::test_confirmed_deletion_is_exact_audited_and_hides_the_reports` |
| Deletion and audit are atomic | `integration/test_report_deletion.py::test_audit_insert_failure_rolls_back_deletion_and_consumption`, `::test_concurrent_confirmations_delete_and_audit_once`; `unit/reports/test_report_deletion.py::test_audit_failure_leaves_the_report_alive` | `::test_proposal_is_retry_safe_and_single_use_in_the_database` |
| Over HTTP, on both backends | `integration/test_http_api.py::test_reports_export_and_deletion_are_owner_only` (temporal); the same test via `integration/test_local_http_api.py` (local) | same test: the owner's confirmed deletion succeeds |

### Additional release gates

| Gate | Cases | Positive control | Status |
| --- | --- | --- | --- |
| 1. Revoked evidence leaves history by provenance | `unit/security/test_release_gates.py::test_g1_withdrawn_conclusions_leave_history_by_provenance` (a percentage, a small integer and a qualitative ranking); `::test_g1_generated_text_without_provenance_fails_closed`; `unit/context/test_context_builder.py::test_scope_narrowed_mid_session_removes_facts_from_evidence_and_history`; `unit/test_guarded_model_history.py::test_stale_history_never_reaches_provider`; `integration/test_report_reuse_withdrawal.py::test_deletion_withdraws_links_and_excludes_dependent_answers` | the same message is kept before narrowing; a linked answer is kept with no access change | met |
| 2. Authority rechecked at release | `unit/security/test_release_gates.py::test_g2_release_recheck_blocks_cited_and_recognisable_figures`; `integration/test_security_release_gates.py::test_cited_answer_is_withheld_after_mid_generation_revocation` (local); `integration/test_investigations.py::test_release_masks_email_and_withholds_revoked_evidence`, `::test_fresh_authority_at_model_tool_and_release_boundaries` (temporal); `unit/http/test_conversations.py::test_authority_is_rechecked_on_every_batch`; `unit/http/test_api.py::test_sse_stops_when_authority_is_revoked_mid_stream`; uncited conclusions (G-1, fixed): `unit/security/test_release_gates.py::test_g2_uncited_conclusion_from_revoked_run_evidence_is_withheld`, `unit/privacy/test_run_evidence_release.py` (release on every destination, re-display of answer and question, unloadable linked evidence), `integration/test_security_release_gates.py::test_uncited_answer_is_withheld_after_mid_generation_revocation`, `::test_released_answer_is_withheld_on_redisplay_after_revocation` (local) | `integration/test_security_release_gates.py::test_answer_released_under_unchanged_access`; `unit/privacy/test_run_evidence_release.py::test_rule_is_scoped_to_the_run_being_released` | met |
| 3. Names in user, retrieved and generated text | `unit/security/test_release_gates.py::test_g3_cued_names_are_masked_and_brands_or_places_are_not`; `unit/context/test_output_gate.py::test_names_the_user_typed_cannot_be_echoed_later`; Golden: `unit/security/test_release_gates.py::test_g3_golden_text_naming_a_customer_is_refused`, `unit/test_sensitive_content.py::test_cued_person_names_are_flagged`, `unit/test_retrieval.py::test_named_person_example_is_refused_at_draft`, `::test_retrieved_examples_pass_the_context_screen` | brand and place names are released unmasked (report destination); `::test_g3_golden_screen_positive_control`; `unit/test_sensitive_content.py::test_brands_places_and_method_text_are_not_names`; the unnamed example still publishes and is retrieved unchanged | met (cue-based; residual risk below) |
| 4. Analysis-only scope is not the authorization boundary | `unit/security/test_release_gates.py::test_g4_mixed_requests_with_an_off_topic_task_are_declined`, `::test_g4_admitted_injection_still_cannot_cross_the_product_boundary`; `unit/context/test_request_scope.py` | `unit/context/test_request_scope.py::test_analysis_and_administration_proceed` | met (classifier is coarse; see limits) |

## Open findings

- **G-1, fixed.** Release used to recheck only cited evidence IDs and
  figures it could recognise as withdrawn (numbers of 1,000 and above), so
  an uncited answer stating only a percentage, a small integer or a
  qualitative conclusion ("your strongest product is up 12% on 7 orders")
  was released after the executive's products were narrowed. The output
  gate now also checks every evidence record linked to the run (its trusted
  run links, produced or reused, cited or not). If any of them is withheld
  by authority now, or can no longer be loaded, nothing generated for that
  run is released, on every destination. At release the runtime stops the
  run with the access-changed notice (shared by the local and Temporal
  backends); on re-display (`run_view`, event replay) the answer and open
  question are replaced by the withheld notice. Evidence that is only
  superseded (changed definitions, same authority) does not trigger it.
  Cases: see gate 2 above.
- **G-2, fixed.** The Golden knowledge screen now uses the same cue-based
  name detector as the context and output screens, so an example naming a
  person ("revenue from the customer named …") is refused when it is
  submitted, on every publication path including the local administrator's
  self-publish. Retrieved examples also pass the context screen before they
  reach the model: names and opaque references are masked. This covers
  examples stored before the detector existed. A name without a cue is still
  not detected (see Coverage limits); human review remains the safeguard
  for it.

## Coverage limits

- **Where names can and cannot come from.** Query results never contain
  person names or contact details. The catalog lists them in
  `DIRECT_IDENTIFIER_COLUMNS` (first and last name, email, street address,
  postal code, city, coordinates) with no permitted derivation, so no field
  can be built on them; the SQL compiler refuses any field sourced from them
  (`adapters/sql_compiler/bindings.py`); and the result privacy boundary
  re-checks every output's lineage before rows leave
  (`application/result_privacy.py`). No data can therefore be looked up,
  filtered or linked by a person's name. The cue-based name detector is a
  second line of defence for text people supply (chat messages, Golden
  examples): a name reaches an answer or report only if someone typed it
  (or the model invents one).
- **Name detection is contextual, not comprehensive.** Names are detected
  after a cue such as "customer named", "Mr." or "client called", and a
  name the user typed with such a cue is remembered as a protected term and
  masked if it is echoed later. The detector also uses an optional
  known-name list, which is empty by default. A name without a cue is not
  detected: in "How much did customer Maria Lopez spend?" the name is not
  recorded as a protected term, so a model answer that repeats "Maria
  Lopez" is not masked (no data about her can be returned; only the typed
  name is echoed). The same holds for a bare name in generated prose or in
  Golden knowledge ("Maria Lopez bought the most").
- **User-typed quotes.** After narrowing, the user's own earlier messages are
  kept with every figure removed, but their qualitative wording (for example
  "Beta is strongest") stays. This is by design: the user's own words are
  kept, and dependent assistant answers are withheld.
- **No conversation summarization exists.** History is bounded by budget,
  not summarized. Evidence compaction applies only to evidence that is
  usable now, so there is no summary path to verify beyond these.
- **The analysis-only classifier is keyword-based.** Paraphrased off-topic
  requests ("compose some verses about our store") are asked to clarify on a
  cold start and proceed inside an ongoing investigation. The model
  instructions then apply. Authorization never depends on this
  classification.
- **Report-title disclosure after narrowed access is deferred.** Report
  reads, exports, listings, search and deletion previews withhold the title.
  Provenance recorded when report evidence was linked into another
  conversation keeps the title it had then. This record makes no
  post-revocation claim for titles (see
  [known limitations](architecture/known-limitations.md)).
- Live BigQuery and model-provider runs are outside this record. All cases
  use fixture data and scripted models.
