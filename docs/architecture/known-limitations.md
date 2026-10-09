# Known limitations and deferred work

These are the known gaps of the current build. Each one is a disclosure, not
a passing check.

## Deferred (agreed, not implemented)

| Item | What exists | What is deferred |
| --- | --- | --- |
| System-level learning loop | Versioned, reviewed Golden examples; persona rollback; versioned retrieval configuration; evaluation runner | Automatic candidate generation from interactions, evaluation-gated promotion and monitored rollback (design in [requirements](requirements.md#4b-system-level-planned-implementation-deferred)) |
| Report-title disclosure after access narrows | Report reads, exports, listings, search and deletion previews withhold the title once the owner's current products no longer cover the report | Not every surface that recorded a title applies that rule. For example, provenance stored when a report's evidence was linked into another conversation keeps the title it had then. After access narrows, a title can still be shown there. **Complete post-revocation protection is not claimed.** Deferred for the local demo. |
| Multi-period evidence metadata | Evidence records one compiler-derived period, and only when every dated read uses the same window | A query that compares several periods has no single recorded period and no explicit multi-period marker. Reports and answers must name the compared periods themselves. |
| Two-model judge calibration | Deterministic checks; human review of reports (pending) | Calibrating two model judges against human-reviewed controls, and repeat-consistency checks. Judge-scored results stay blocked unless a judge actually runs. |
| Exhaustive fault matrix | Representative fault-injection tests for queries, providers, workers and the local process | Every combination of faults |

## Pending results

The compact real-model evaluation, the human report review, the focused
security verification and the human CLI walkthrough are pending. No outcome
is claimed for them until their results are published in the repository.

## Implemented behaviour with known limits

- **Report access after a scope change.** A saved report is readable while
  the owner's current products cover its required scope. That scope is the
  union of the exact product sets its evidence was computed under, taken
  from trusted execution metadata. Widening access keeps reports readable.
  Removing a required product blocks them (`ACCESS_CHANGED`). Report
  versions saved before database migration 0015 get a required scope only
  when it can be proven from current entitlements. The rest keep the older
  strict rule: readable only while the owner's product set is exactly the
  one at save time. Saving a new version from a fresh analysis restores
  access.
- **Local execution mode.** Best effort in a single process. A crash is
  recorded at the next start. One manager per database is guaranteed only
  by an advisory lock that is lost silently if its connection drops. A
  queued-request discard and its notice are two separate writes. See
  [production deployment](production-deployment.md#local-mode-limits-implemented-accepted-for-the-local-demo).
- **Privacy.**
  - The policy pseudonymizes; it does not anonymize.
  - Names in free text are found by cues and by exact terms, not by a
    general detector.
  - Derived figures from withheld evidence cannot be recognized.
  - BigQuery job metadata holds query parameters.
- **Golden retrieval.**
  - Small corpus.
  - Relevance labels were written by the implementing agent alone, before
    any run. There is no second annotator.
  - No relevance measurement on live traffic.
- **Benchmarks.**
  - Expected values are reproduced by two SQL routes, but no named human
    reviewer has signed them off.
  - The public dataset is regenerated regularly, so live runs are smoke
    tests unless a drift report shows no drift.
  - Whether the sanitized frozen extract may be redistributed is reviewed
    before any public release.
- **Persona preview.** The offline renderer shows the layout and the
  instruction block, without a model applying the style.
- **Progress events.** Model retries and provider switches are visible in
  traces and metrics, not in the user's progress stream.
- **Operations.**
  - No alert rules and no telemetry retention policy.
  - Report restore is an operator command.
  - Backups and other retained copies are not covered by purge.
- **Production.** The reference deployment is a design. Nothing is
  provisioned, load-tested or security-tested in the cloud.
