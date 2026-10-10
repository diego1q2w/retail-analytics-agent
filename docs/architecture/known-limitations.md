# Known limitations and deferred work

These are the known gaps of the current build. Each one is a disclosure, not
a passing check.

## Deferred (agreed, not implemented)

| Item | What exists | What is deferred |
| --- | --- | --- |
| System-level learning loop | Versioned, reviewed Golden examples; persona rollback; versioned retrieval configuration; evaluation runner | Automatic candidate generation from interactions, evaluation-gated promotion and monitored rollback (design in [requirements](requirements.md#4b-system-level-planned-implementation-deferred)) |
| Report-title disclosure after access narrows | Report reads, exports, listings, search and deletion previews withhold the title once the owner's current products no longer cover the report | Not every surface that recorded a title applies that rule. For example, provenance stored when a report's evidence was linked into another conversation keeps the title it had then. After access narrows, a title can still be shown there. **Complete post-revocation protection is not claimed.** Deferred for the local demo. |
| Multi-period evidence metadata | Evidence records one compiler-derived period, and only when every dated read uses the same window | A query that compares several periods has no single recorded period and no explicit multi-period marker. Reports and answers must name the compared periods themselves. |
| Two-model judge calibration | Deterministic checks; the judge harness and the `report-quality-v1` rubric ([judges and rubrics](../../evaluation/judges/README.md)); a human report review packet (review pending); the offline and continuous judge design in [production deployment](production-deployment.md#quality-evaluation-and-model-judges-proposed) | A judge scorer, running two model judges, calibrating them against human-reviewed controls, and repeat-consistency checks. Judge-scored results stay blocked unless a judge actually runs. |
| Exhaustive fault matrix | Representative fault-injection tests for queries, providers, workers and the local process | Every combination of faults |

## Evaluation and verification results

- **Real-model evaluation (measured).** Ten conversations on one day, on
  the local backend, over the frozen extract and the held-out fixture:
  [evaluation/real-model](../../evaluation/real-model/README.md). Gemini
  answered all ten. Judge-scored dimensions stay unscored. It is a
  measurement of ten conversations, not a quality threshold.
- **Human report review (pending).** The rubric and verdict sheet are in
  [evaluation/real-model/human-review](../../evaluation/real-model/human-review/README.md).
  No person has reviewed the reports yet.
- **Security verification (done).** All four release gates are met:
  [security verification](../security-verification.md).
- **Recovery (tests done; human walkthrough pending).** The recovery tests
  run in the repository checks. The hands-on CLI walkthrough is written but
  has not been run by a person yet:
  [recovery walkthrough](../recovery-walkthrough.md).
- **Release audit.** What was checked before release, and what remains
  open: [release verification](../release/verification.md).

Live agent runs before the Gemini schema fix described in the
[real-model evaluation](../../evaluation/real-model/README.md#gemini-fix-found-by-this-evaluation)
were answered by the GPT backup, not by Gemini, even where they were taken
to be Gemini runs. Only the real-model evaluation above is a
verified Gemini result.

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
  [production deployment](production-deployment.md#limits-of-the-simpler-manager-implemented).
- **Privacy.**
  - Customer demographics are aggregate-only, with no minimum group size:
    this is not anonymization. A naturally small group (even one customer)
    is released as a group statistic; selecting people by reference or by
    rank is refused. Open ambiguous case: grouping by many fine keys (state,
    age band, product, order date) can yield single-person groups without
    any reference; the grain check cannot tell this from an ordinary
    breakdown.
  - Evidence and reports recorded under the earlier rule (individual
    demographics allowed) are withheld when they cannot be verified as
    group-level; they are not rewritten or deleted. Traces and logs from
    before the change may still hold individual demographics until an
    operator removes them manually.
  - The output gate does not parse generated text for a reference written
    next to a demographic. Released data never pairs them, so such text can
    only come from what a user typed or what the model invents.
  - The grain check fails closed: a structure it cannot follow is refused,
    and a demographic in any clause of a query whose rows are individual
    (even through an unrelated subquery) refuses the whole query.
  - Query results never contain person names or contact details: the
    catalog marks them `DIRECT_IDENTIFIER_COLUMNS` with no permitted
    derivation, the SQL compiler refuses them
    (`adapters/sql_compiler/bindings.py`) and the result privacy boundary
    re-checks (`application/result_privacy.py`). No data can be looked up or
    linked by a person's name; a name reaches an answer or report only if
    someone typed it (or the model invents one).
  - The name detector is a cue-based second line of defence for text people
    supply (chat messages, Golden examples), not a general detector. A name
    typed without a cue ("How much did customer Maria Lopez spend?") is not
    recorded as a protected term, so a model echo of that typed name is not
    masked.
  - Derived figures from withheld evidence cannot be recognized by the
    figure check; instead, nothing generated for a run is released while any
    evidence linked to that run is withheld.
  - BigQuery job metadata holds query parameters.
- **Brand-access lifecycle.** Managers are authorized by brand, resolved
  by exact name match against a synced catalog snapshot. Who administers
  assignments in production, approval, identity-provider synchronization,
  merging brand spelling variants and automatic grants for new products are
  open; the CEO-equivalent identity holds an explicit list of today's
  product IDs, not an "all products" entitlement
  ([brand-based access](../brand-access.md)).
- **Golden retrieval.**
  - Small corpus.
  - Relevance labels were written by the implementing agent alone, before
    any run. There is no second annotator.
  - No relevance measurement on live traffic.
- **Skill selection and Golden invocation need more testing.** Retrieval
  works when called, and the investigation skill instructs the model to
  consult reviewed examples for driver investigations and reports that need
  new analysis. How reliably that happens on unprompted questions has not
  been measured in repeated trials; the existing retrieval scores measure
  what is returned, not the decision to call. The skill's guidance and its
  tests are where this is strengthened. Production needs labelled, repeated
  evaluations of selection precision and recall for Golden retrieval and the
  other skills, plus answer quality, latency and cost. See
  [production evaluation](production-deployment.md#production-evaluation-of-skill-selection).
- **One growing context.** Each model request carries the run's history
  (compact tool results and evidence IDs, not rows). Long investigations
  approach the 100k token budget, and the run then ends with its verified
  findings. Bounding context growth (compacting older tool results,
  summarizing settled sub-questions) is planned work; see
  [agent topology](technology-choices.md#agent-topology-one-adaptive-loop-with-skills-implemented).
- **Benchmarks.**
  - Expected values are reproduced by two SQL routes, but no named human
    reviewer has signed them off.
  - The public dataset is regenerated regularly, so live runs are smoke
    tests unless a drift report shows no drift.
  - Whether the sanitized frozen extract may be redistributed has not been
    confirmed. Public release is blocked until it is (see
    [release verification](../release/verification.md)).
- **Persona preview.** The offline renderer shows the layout and the
  instruction block, without a model applying the style.
- **Progress events.** Model retries and provider switches are visible in
  traces and metrics, not in the user's progress stream.
- **Operations.**
  - No alert rules and no telemetry retention policy.
  - Traces hold sanitized but still sensitive analytical content; the local
    MLflow has no access control beyond listening on localhost. Payloads
    over the size bounds are truncated with a marker, not stored elsewhere.
    Names typed without a cue are not detected in captured text.
  - Report restore is an operator command.
  - Backups and other retained copies are not covered by purge.
- **Production.** The production deployment is a proposal with open service
  decisions. Nothing is provisioned, load-tested or security-tested in the
  cloud.
