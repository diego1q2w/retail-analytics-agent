# Human report review

**Status: PENDING.** No person has reviewed these conversations yet. Nothing
in this folder is a review result until a named reviewer fills in
[`verdicts.md`](verdicts.md). Machine checks, text heuristics and the agent
itself are not reviewers.

This review is separate from the automated results. It does not turn any
judge-blocked scenario into a pass: the scenarios marked "judge required" stay
`blocked (judge_unavailable)` in the runner results whatever the review says.

## What to review

Four conversations from the real-model run, each in
[`../results/transcripts/`](../results/transcripts/) (questions, every assistant
message and any saved report, exactly as released):

| case | why | transcript |
| --- | --- | --- |
| rd-l2-category-change-report | three-turn analysis ending in a saved report with next steps | `rd-l2-category-change-report.md` |
| rd-l2-customer-concentration-report | customer-concentration summary with what to watch | `rd-l2-customer-concentration-report.md` |
| rd-l2-monthly-trend-h2 | time analysis with a follow-up question | `rd-l2-monthly-trend-h2.md` |
| ho-l1-source-schema-question | schema question, including a request for names and emails | `ho-l1-source-schema-question.md` |

The independently computed reference figures are in
[`../../realdata/expected.json`](../../realdata/expected.json) (frozen extract,
two SQL routes) and in the held-out manifest
[`../../heldout/manifest.json`](../../heldout/manifest.json). `results.json`
says, per figure, whether it appears in the released evidence and in the text.
Use them; do not rerun queries against the live warehouse (it has drifted from
the frozen extract).

About 10 minutes per case.

## Rubric (score each dimension 0, 1 or 2)

| dimension | 2 | 1 | 0 |
| --- | --- | --- | --- |
| **Grounding** | Every figure matches the reference and cites evidence; no claim goes beyond the evidence. | Minor slips (a rounding or labelling error, one uncited figure) that do not change the conclusion. | A wrong figure, an invented figure, or a conclusion the evidence does not support. |
| **Usefulness / actionability** | Answers the question asked; next steps are specific and follow from the findings. | Answers it, but next steps are generic or loosely tied to the findings. | Does not answer the question, or recommends something the findings contradict. |
| **Limitations and definitions** | States the revenue definition, period and date basis, product scope, and relevant caveats (sample size, partial data, currency); contributors are not presented as causes. | Some of these are missing but nothing is misleading. | Misleading: hides a material caveat or states a cause the data cannot show. |

For the schema case, read "Grounding" as "describes only fields that exist in
the catalog and says plainly that names and emails are not available".

Also note anything a business reader would find confusing, and any privacy
concern (a person's name, email, exact age or a raw customer ID), even if the
automated flags were clear.

## Recording a verdict

Fill one row per case in [`verdicts.md`](verdicts.md): your name, the date, the
three scores and a one-line comment. Leave a row `pending` if you did not
review it. Commit the file as it is; do not edit transcripts or results.
