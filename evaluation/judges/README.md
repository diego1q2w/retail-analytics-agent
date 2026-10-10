# Model judges and rubrics

The single description of the judges this project uses or proposes: what is
implemented, what the rubric dimensions mean, how judges are calibrated, and
what they never decide. The production design that relies on it is in
[quality evaluation and model judges](../../docs/architecture/production-deployment.md#quality-evaluation-and-model-judges-proposed).

## Status

| Part | Status |
| --- | --- |
| Judge harness: `JudgeScorer` port, `JudgeSpec` per scenario, `JudgeScore` results in their own non-gating section, `judge_unavailable` / `judge_error` statuses | **Implemented** (`application/ports/evaluation.py`, `application/evaluation/manifest.py`, `results.py`, `runner.py`) |
| Rubric `report-quality-v1`: six dimensions named on 9 held-out and 2 real-data scenarios | **Implemented** in the manifests; the scoring anchors below are written for it now and have not yet been applied by any judge |
| A judge scorer (a model applying the rubric) | **Not implemented.** The only `JudgeScorer` is a stub in a unit test. `retail-analytics-eval` wires no judges, so the 11 judge-required scenarios are `blocked (judge_unavailable)` |
| Calibration against human-reviewed controls | **Pending**: the [human review packet](../real-model/human-review/README.md) has no verdicts yet |
| Rubric `report-quality-v2` and the further judges below | **Proposed** |

Nothing in this file turns a blocked scenario into a pass.

## Division of labour

Judges score what a literal check cannot read. They never decide a security
or a numeric result, and a high judge score never overrides a failed
deterministic check.

| Deterministic only (release gates) | Model judges |
| --- | --- |
| Product scope and privacy: no out-of-scope data, no personal data, aggregate-only demographics (fixture canaries, compiler and gate tests) | Whether the text answers the question asked, at the detail asked |
| Numbers: every expected figure present in the released evidence and stated in the text (`real_model.py` figure checks, two independent SQL routes) | Whether every claim is supported by cited evidence, and nothing goes beyond it |
| Deletion only after an explicit typed confirmation; budgets; recovery; idempotency | Whether definitions, periods, scope and caveats are disclosed; contributors kept apart from causes; actions follow from findings |
| Skill and Golden selection precision/recall (labelled traces, counted) | Whether a clarification was necessary; whether a report follows the pinned persona |

## Rubric `report-quality-v1` (implemented names, anchors written now)

Each dimension is scored 0, 1 or 2. Every score must cite the evidence ID
or the passage it is based on (`evidence_refs`), so a reviewer can check it.
The judge receives an **evidence packet**: the question and its
clarifications, the released answer or report exactly as the user saw it,
the released rows of every cited evidence record, the definitions and
preferences in force, and the persona version. It sees the same masked data
the user did and nothing else.

| Dimension | 2 | 1 | 0 |
| --- | --- | --- | --- |
| `definition_disclosure` | States the metric definition (for example completed-item sales), the period and date basis, the product scope and the currency status | One of these is missing, and nothing is misleading | The reader would take a different definition, period or scope than the one used |
| `limitations` | Names the caveats that matter for this answer: partial or unequal periods, sparse data or small groups, empty results explained, unverified currency, withheld evidence | A material caveat is missing but the conclusion still holds | A material caveat is hidden, or a caveat is stated that contradicts the evidence |
| `evidence_support` | Every figure and label is in a cited evidence record; no qualitative claim exceeds what the rows show | One uncited or loosely supported statement that does not change the conclusion | A figure or claim the cited evidence does not contain, or a citation to evidence that does not support the sentence |
| `contributors_not_causes` | Measured differences and contributors are reported as such; explanations beyond the data (seasonality, marketing, traffic) appear only as labelled hypotheses, in headings and actions too | A hypothesis is labelled in the body but stated as fact in a heading or an action | The text asserts a cause the transaction data cannot show |
| `action_items` | Recommended actions are specific, follow from the findings and respect the stated limitations | Actions are generic or loosely tied to the findings | Actions are missing where requested, or contradict the findings |
| `schema_accuracy` | Describes only fields and relations that exist in the approved catalog, and says plainly what is not available (for example names and e-mails) | A field is described imprecisely but exists | A field, relation or capability is invented, or unavailable data is promised |

Dimensions are scored only where the scenario names them (the manifests list
one to five per scenario). The score of a dimension is the lower of the two
judges' scores when they differ by one, and "disagreement" when they differ
by two: disagreements are never averaged.

### Mapping to the human review rubric

The [human review packet](../real-model/human-review/README.md) scores three
coarser dimensions. They map onto the judge rubric as follows, so the human
verdicts can calibrate the judges without a second review:

| Human dimension | Judge dimensions |
| --- | --- |
| Grounding | `evidence_support`, `schema_accuracy` |
| Usefulness / actionability | `action_items` (and `intent_match` in v2) |
| Limitations and definitions | `definition_disclosure`, `limitations`, `contributors_not_causes` |

A judge whose dimension scores, combined this way, disagree with the human
verdict on a control case by two points is not calibrated.

## Rubric `report-quality-v2` (proposed)

Version 2 keeps every v1 dimension unchanged (so v1 results stay comparable)
and adds the two things requirement 6 asks about that v1 does not score:

| Dimension | 2 | 1 | 0 |
| --- | --- | --- | --- |
| `intent_match` | Answers the question that was asked, for the population, period and metric the user meant (after any clarification), in the form asked (a figure, a comparison, a report) | Answers a close neighbour of the question or adds an unrequested interpretation, but the asked answer is present | Answers a different question, or asks for a clarification the context already resolved |
| `proportionality` | The length and detail match the request: a figure question gets a figure with its definition; a report gets findings, limitations and actions; no unrequested breakdowns | Some unrequested detail, still easy to find the answer | The answer is buried in unrequested analysis, or a requested part is missing |

Manifests adopt v2 by changing `rubric_id`; a scenario may stay on v1.

## Further judges (proposed for production)

| Judge | Question it answers | Why it is a judge and not a check | Calibration controls |
| --- | --- | --- | --- |
| **Report quality** (rubric above) | Does a released answer or report meet the rubric? | The dimensions are about meaning, not literal strings | Human review packet plus deliberately flawed variants of real transcripts: a changed figure, a removed definition, an action without a finding, a hypothesis written as a cause, an invented field |
| **Persona adherence** | Does a sampled report follow the pinned persona version (tone, detail level, layout, terminology)? | The persona is free text; the offline preview shows the instruction block but does not apply the style with a model ([requirement 8 limits](../../docs/architecture/requirements.md#8-agility-persona-management)). Figures, evidence IDs and limitations are compared deterministically before the judge runs; the judge scores only style | The same findings rendered under two published persona versions; a report that follows the old persona must score low against the new one |
| **Clarification necessity** | When the agent asked a question, was the ambiguity material (different answers depending on the choice), and when it did not ask, should it have? | Materiality is a judgement about the question and the data, not a pattern | Held-out cases labelled "clarify" / "do not clarify" by a human; the labelled set is also what the deterministic clarification-rate metric is compared against |
| **Pairwise comparison** | Given the same scenario answered by two configurations (model tier, prompt, skill version, Golden corpus), which answer is better on each rubric dimension, or are they equivalent? | Rollouts compare two candidates, and absolute scores drift; a preference with a stated reason is more stable | Every pair is judged twice with the positions swapped; a judge that changes its preference with the order is discarded for that pair. Known-better/known-worse pairs built from the flawed variants above |

Not judges, on purpose:

- **Skill and Golden selection.** Precision and recall of `load_skill` and
  `find_analysis_examples` calls are counted from traces against a labelled
  set ([production evaluation of skill selection](../../docs/architecture/production-deployment.md#production-evaluation-of-skill-selection)).
- **Refusals and scope.** Whether an off-topic or out-of-scope request was
  refused, and whether anything leaked, is deterministic (admission, compiler,
  gate and fixture canaries). A judge may at most score the tone of a
  refusal under the persona judge.
- **UX.** The human CLI walkthrough and the operational metrics (time to
  first progress, time to answer, completed, partial and failed runs,
  clarification rate).

## Judge models and protocol

- **Two judges, different vendors**, at least one not the vendor of the
  agent's primary model, so a shared blind spot between the agent and its
  judge is less likely. The project's provider adapters give a Gemini and a
  GPT candidate; a Claude model is a candidate for the second seat if an
  adapter is added. Exact models and versions are chosen when calibration is
  run and are recorded in every result file (`judge_ids`), like the agent's
  model today.
- **Independent scoring.** Neither judge sees the other's output. Both get
  the same versioned rubric and the same evidence packet.
- **Calibration before thresholds.** The rubric is run first on the
  human-reviewed controls and the flawed variants. A judge that passes a
  flawed control, or disagrees with a human verdict by two points, is not
  used until the rubric or the judge is fixed.
- **Consistency.** Selected cases are repeated to measure within-judge
  consistency; the same cases across both judges measure agreement.
  Agreement is consistency, not truth: disagreements and consistent-but-wrong
  results go to a person.
- **Thresholds** for the analytical dimensions are set from the calibrated
  baseline and recorded with the code, model, prompt, persona, catalog,
  policy and retrieval versions, and only then used as release criteria.
- **Where they run.** Offline on the held-out and real-data suites before a
  release, after any change to the model, prompts, skills, persona defaults
  or Golden corpus; in production on a sample of completed runs taken from
  the sanitized traces, outside the request path (no latency or spend added
  to a user's question). Sampled scores feed the monitor stage of the
  learning loop; a drop against the baseline is a signal for human review and,
  if confirmed, a rollback. Judges are never an automatic promotion gate on
  their own.
- **Costs and limits.** Judge calls are model spend outside the per-question
  budget and are metered separately. Judges inherit model biases (verbosity,
  self-preference, position); calibration, position swapping and the
  different-vendor rule limit but do not remove them.

## Wiring a scorer

A scorer implements `JudgeScorer` (`application/ports/evaluation.py`):
`judge_id`, and `score(case, observation, rubric_id, dimensions)` returning
one `JudgeScoreOut` per dimension with `evidence_refs`. It is passed to
`run_manifest(..., judges=[...])`; the runner records `judge_ids` in the
result and scores every scenario whose `verification` includes `judge`. The
`retail-analytics-eval` command has no judge option yet; adding one is part
of implementing the first scorer. The adapter belongs in
`adapters/evaluation/`, builds the evidence packet from the observation and
the released evidence only, and sends nothing a user would not have seen.
