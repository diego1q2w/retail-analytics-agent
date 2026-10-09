# Golden retrieval benchmark

Measured precision, recall and ranking of Golden example retrieval, with keyword-only, semantic-only and fused variants compared on the same eligible corpus. Everything here is synthetic. The numbers below are real measurements from one run (embedding model `gemini-embedding-2`, 768 dimensions, Gemini free tier); rerun them with the commands at the end.

## What was labeled

- **Corpus** (19 published examples, 18 deliverable): the ten project-authored seeds (`application/golden_seed_library.py`) plus nine evaluation-only examples in `corpus.json`: three eligible examples on other methods (return rate, average order value, repeat customers), two restricted to menswear products, one restricted to womenswear products, one on revenue definition version 2 (incompatible), one on logical schema version 2 (incompatible) and one that was published and then retired. All use schema `logical-catalog/1` and the current metric versions unless they are the incompatible ones.
- **Questions** (`labels.json`): 77 questions, 39 tuning and 38 held-out, in these categories: paraphrases of one intent, direct questions, ambiguous questions, incompatible definition or schema, retired, unauthorized (the asker lacks the products), authorized restricted, and no match. Three askers: all products, womenswear only, menswear only.
- **Relevance guideline**: judged by method, not wording. Grade 2 means the example's method applies as written; grade 1 means it applies in part or with a definition caveat; absent means it does not apply. A restricted example is "relevant" only for askers who may receive it. Each question also lists examples that must never be returned to that asker.
- **Labeler**: one person (the implementing agent), writing grades from the example questions and method summaries before any retrieval run. There is no second annotator and no agreement statistic. Labels were not edited after seeing any result.
- **Split**: whole intent groups (paraphrases together) belong to one split. A test fails if any held-out question has Jaccard similarity of 0.5 or more (content words) with a tuning question, a seed question or an evaluation-example question. The largest observed similarity between a held-out and a tuning question is 0.40. Seed questions never appear as held-out questions.

## Metrics and k

k is 1 and 3 (retrieval returns at most 3 examples). Precision@k is the share of returned top-k examples that are relevant; a question that returns nothing has no precision and counts as a decline. Recall@k is relative to the known relevant examples the asker may receive. MRR is the reciprocal rank of the first relevant example (0 if none returned). nDCG@3 uses gain 2^grade - 1. These four are defined only for questions with at least one authorized relevant example. No-match behavior is correct when a question with no suitable example returns nothing. "False declines" counts questions that expected a match but got nothing. Access violations count returned examples that the asker may not receive, the example is retired, or its schema or metric version does not match; exposure is judged from the corpus metadata by code that does not share logic with the retriever, and any hand-listed forbidden example also counts. Each mean shows a 95% percentile bootstrap interval (2000 resamples, fixed seed) and its denominator n.

## Variants

"open" keeps every candidate (up to 3) so it compares ranking only. "placeholder" uses the T24 defaults (min similarity 0.55, min lexical coverage 0.5). "tuned" uses thresholds chosen on the tuning split only (below), with equal-weight fusion, exactly as T36 measured it. "placeholder" always means the old 0.55 / 0.5 values (`PLACEHOLDER_*` in `domain/retrieval.py`). T36-F1 adds "fused-weighted-open" (weighted fusion, open thresholds) and "fused-default" (the shipped defaults: min similarity 0.70, min lexical coverage 0.75, semantic weight 2). Semantic-only and keyword-only drop the other channel; fused is BM25 plus vectors with reciprocal rank fusion (k=60), equal weight unless the variant name says weighted or default. Keyword-only needs no provider; its best tuning coverage (0.5) equals the placeholder, so it has no separate tuned row.

## Results on the held-out split

| variant | split | P@1 | P@3 | R@3 | MRR | nDCG@3 | no-match correct | false declines | access violations |
|---|---|---|---|---|---|---|---|---|---|
| keyword-open | heldout | 0.45 [0.28-0.62] n=29 | 0.22 [0.15-0.29] n=29 | 0.57 [0.39-0.74] n=31 | 0.49 [0.33-0.66] n=31 | 0.51 [0.35-0.67] n=31 | 1/7 | 0/28 | 0 in 0/38 questions |
| semantic-open | heldout | 0.84 [0.71-0.97] n=31 | 0.34 [0.30-0.40] n=31 | 0.91 [0.82-0.98] n=31 | 0.90 [0.80-0.98] n=31 | 0.89 [0.80-0.96] n=31 | 0/7 | 0/28 | 0 in 0/38 questions |
| fused-open | heldout | 0.61 [0.45-0.77] n=31 | 0.26 [0.19-0.31] n=31 | 0.69 [0.53-0.85] n=31 | 0.67 [0.50-0.82] n=31 | 0.66 [0.50-0.81] n=31 | 0/7 | 0/28 | 0 in 0/38 questions |
| keyword-placeholder | heldout | 0.67 [0.33-0.89] n=9 | 0.46 [0.20-0.74] n=9 | 0.19 [0.06-0.32] n=31 | 0.19 [0.06-0.32] n=31 | 0.19 [0.06-0.32] n=31 | 6/7 | 19/28 | 0 in 0/38 questions |
| semantic-placeholder | heldout | 0.84 [0.71-0.97] n=31 | 0.34 [0.30-0.40] n=31 | 0.91 [0.82-0.98] n=31 | 0.90 [0.80-0.98] n=31 | 0.89 [0.80-0.96] n=31 | 0/7 | 0/28 | 0 in 0/38 questions |
| fused-placeholder | heldout | 0.77 [0.61-0.90] n=31 | 0.33 [0.29-0.39] n=31 | 0.88 [0.77-0.97] n=31 | 0.84 [0.73-0.95] n=31 | 0.84 [0.74-0.93] n=31 | 0/7 | 0/28 | 0 in 0/38 questions |
| semantic-tuned | heldout | 0.84 [0.68-0.96] n=25 | 0.65 [0.52-0.78] n=25 | 0.76 [0.61-0.89] n=31 | 0.72 [0.56-0.87] n=31 | 0.73 [0.58-0.87] n=31 | 7/7 | 3/28 | 0 in 0/38 questions |
| fused-tuned | heldout | 0.84 [0.68-0.96] n=25 | 0.65 [0.52-0.78] n=25 | 0.76 [0.61-0.89] n=31 | 0.72 [0.56-0.87] n=31 | 0.73 [0.58-0.87] n=31 | 7/7 | 3/28 | 0 in 0/38 questions |
| fused-weighted-open | heldout | 0.61 [0.45-0.77] n=31 | 0.30 [0.25-0.34] n=31 | 0.82 [0.68-0.94] n=31 | 0.72 [0.58-0.84] n=31 | 0.73 [0.60-0.85] n=31 | 0/7 | 0/28 | 0 in 0/38 questions |
| fused-default | heldout | 0.84 [0.68-0.96] n=25 | 0.65 [0.52-0.78] n=25 | 0.76 [0.61-0.89] n=31 | 0.72 [0.56-0.87] n=31 | 0.73 [0.58-0.87] n=31 | 7/7 | 3/28 | 0 in 0/38 questions |

Reading it: semantic-only ranks best when thresholds are open (MRR 0.90, nDCG@3 0.89). The default RRF fusion ranks lower than semantic-only (MRR 0.67 open, 0.84 with placeholder thresholds) because the keyword channel adds weaker candidates. The placeholder similarity of 0.55 never excludes anything with this embedding model (every question cleared it), so placeholder semantic and fused variants decline nothing and get 0 of 7 no-match questions right. The tuned thresholds decline all 7 no-match questions and return fewer, purer results (P@3 0.65 against 0.34) at the cost of recall (0.76 against 0.91) and 3 of 28 false declines. Access violations were 0 for every variant on both splits. Fused-tuned and semantic-tuned give identical numbers because the tuned lexical coverage (0.75) almost never admits a keyword-only candidate.

### By category (held-out, small denominators)

fused-tuned on heldout, by question category
| category | questions | R@3 | MRR | returned per question | no-match correct | false declines | access violations |
|---|---|---|---|---|---|---|---|
| ambiguous | 3 | 0.00 (n=3) | 0.00 (n=3) | 0.00 | 0/0 | 0/0 | 0 |
| authorized_restricted | 1 | 1.00 (n=1) | 1.00 (n=1) | 1.00 | 0/0 | 0/1 | 0 |
| direct | 17 | 0.85 (n=17) | 0.85 (n=17) | 1.53 | 0/0 | 1/17 | 0 |
| incompatible | 2 | 1.00 (n=2) | 0.42 (n=2) | 3.00 | 0/0 | 0/2 | 0 |
| no_match | 6 | n/a | n/a | 0.00 | 6/6 | 0/0 | 0 |
| paraphrase | 5 | 0.80 (n=5) | 0.80 (n=5) | 1.60 | 0/0 | 1/5 | 0 |
| retired | 1 | 1.00 (n=1) | 1.00 (n=1) | 3.00 | 0/0 | 0/1 | 0 |
| unauthorized | 3 | 0.50 (n=2) | 0.50 (n=2) | 0.33 | 1/1 | 1/2 | 0 |

semantic-open on heldout, by question category
| category | questions | R@3 | MRR | returned per question | no-match correct | false declines | access violations |
|---|---|---|---|---|---|---|---|
| ambiguous | 3 | 0.44 (n=3) | 0.67 (n=3) | 3.00 | 0/0 | 0/0 | 0 |
| authorized_restricted | 1 | 1.00 (n=1) | 1.00 (n=1) | 3.00 | 0/0 | 0/1 | 0 |
| direct | 17 | 0.94 (n=17) | 0.94 (n=17) | 3.00 | 0/0 | 0/17 | 0 |
| incompatible | 2 | 1.00 (n=2) | 0.42 (n=2) | 3.00 | 0/0 | 0/2 | 0 |
| no_match | 6 | n/a | n/a | 3.00 | 0/6 | 0/0 | 0 |
| paraphrase | 5 | 1.00 (n=5) | 1.00 (n=5) | 3.00 | 0/0 | 0/5 | 0 |
| retired | 1 | 1.00 (n=1) | 1.00 (n=1) | 3.00 | 0/0 | 0/1 | 0 |
| unauthorized | 3 | 1.00 (n=2) | 1.00 (n=2) | 3.00 | 0/1 | 0/2 | 0 |


Ambiguous questions score 0 recall with the tuned settings because the retriever declines them; for those, declining is acceptable, so they are not counted as false declines. Each category has between 1 and 17 questions: do not read category rows as precise rates.

## Results on the tuning split (the settings were fitted here)

| variant | split | P@1 | P@3 | R@3 | MRR | nDCG@3 | no-match correct | false declines | access violations |
|---|---|---|---|---|---|---|---|---|---|
| keyword-open | tuning | 0.56 [0.41-0.75] n=32 | 0.29 [0.23-0.35] n=32 | 0.74 [0.59-0.88] n=33 | 0.63 [0.47-0.77] n=33 | 0.64 [0.49-0.78] n=33 | 1/6 | 0/31 | 0 in 0/39 questions |
| semantic-open | tuning | 0.88 [0.76-0.97] n=33 | 0.36 [0.32-0.41] n=33 | 0.95 [0.87-1.00] n=33 | 0.92 [0.85-0.98] n=33 | 0.92 [0.84-0.99] n=33 | 0/6 | 0/31 | 0 in 0/39 questions |
| fused-open | tuning | 0.70 [0.55-0.85] n=33 | 0.32 [0.27-0.37] n=33 | 0.86 [0.74-0.97] n=33 | 0.79 [0.67-0.89] n=33 | 0.79 [0.67-0.90] n=33 | 0/6 | 0/31 | 0 in 0/39 questions |
| keyword-placeholder | tuning | 0.63 [0.44-0.81] n=27 | 0.49 [0.36-0.62] n=27 | 0.60 [0.43-0.76] n=33 | 0.57 [0.40-0.73] n=33 | 0.55 [0.39-0.71] n=33 | 6/6 | 5/31 | 0 in 0/39 questions |
| semantic-placeholder | tuning | 0.88 [0.76-0.97] n=33 | 0.36 [0.32-0.41] n=33 | 0.95 [0.87-1.00] n=33 | 0.92 [0.85-0.98] n=33 | 0.92 [0.84-0.99] n=33 | 0/6 | 0/31 | 0 in 0/39 questions |
| fused-placeholder | tuning | 0.79 [0.64-0.91] n=33 | 0.32 [0.27-0.37] n=33 | 0.86 [0.74-0.97] n=33 | 0.83 [0.71-0.94] n=33 | 0.82 [0.70-0.92] n=33 | 0/6 | 0/31 | 0 in 0/39 questions |
| semantic-tuned | tuning | 0.90 [0.77-1.00] n=31 | 0.52 [0.42-0.63] n=31 | 0.85 [0.73-0.95] n=33 | 0.86 [0.74-0.97] n=33 | 0.85 [0.72-0.95] n=33 | 6/6 | 1/31 | 0 in 0/39 questions |
| fused-tuned | tuning | 0.81 [0.66-0.94] n=32 | 0.52 [0.42-0.63] n=32 | 0.88 [0.77-0.97] n=33 | 0.85 [0.74-0.94] n=33 | 0.85 [0.74-0.94] n=33 | 6/6 | 0/31 | 0 in 0/39 questions |

| fused-weighted-open | tuning | 0.76 [0.61-0.91] n=33 | 0.35 [0.31-0.39] n=33 | 0.93 [0.85-1.00] n=33 | 0.85 [0.76-0.94] n=33 | 0.85 [0.76-0.94] n=33 | 0/6 | 0/31 | 0 in 0/39 questions |
| fused-default | tuning | 0.84 [0.72-0.97] n=32 | 0.52 [0.42-0.63] n=32 | 0.88 [0.77-0.97] n=33 | 0.86 [0.74-0.95] n=33 | 0.86 [0.75-0.95] n=33 | 6/6 | 0/31 | 0 in 0/39 questions |

## Threshold selection (tuning split only)

The sweep scores each setting by the mean of recall@3, precision@3 (0 when nothing is returned for a question that expects a match) and no-match rate, subject to zero exposure, and breaks ties toward stricter thresholds. Best settings: fused min similarity 0.70 with min lexical coverage 0.75 (0.67 scored the same on tuning); semantic-only min similarity 0.70; keyword-only min lexical coverage 0.5. The held-out result at these settings is above.

**Recommendation** (T36; adopted in T36-F1, see the last section): for `gemini-embedding-2` at 768 dimensions, set `RETRIEVAL_MIN_SIMILARITY` near 0.70; the 0.55 placeholder admits everything. Cosine thresholds depend on the embedding model and the corpus, so recompute them if the model, dimensions or corpus change. The lexical coverage threshold barely matters once the similarity threshold is set. Do not choose fusion over semantic-only on these numbers: fused matched but did not beat semantic-only at tuned thresholds and ranked lower at open thresholds. A reranker was not tested: the remaining losses at the tuned settings are declines (threshold trade-off), not wrong order within returned results, and a rerank step would add latency and cost to a corpus of 18 deliverable examples. Revisit if the corpus grows by an order of magnitude or MRR falls.

## Limits

- Tiny corpus (18 deliverable examples) and 38 held-out questions (31 with a relevant example): intervals are wide, category rows are anecdotes, and an 18-document corpus makes recall@3 forgiving.
- One labeler, no agreement measure. Two held-out questions (rt-h12, rt-h28) returned an authorized menswear example that is arguably relevant but not graded; precision is slightly understated.
- Paraphrases and held-out questions were written by the same author as the labels and seeds, so wording may be closer to the corpus than real executive questions.
- Thresholds were tuned and evaluated on a small split of the same corpus; the fused-tuned recall@3 falls from 0.88 on tuning to 0.76 held-out, which shows how fragile that is.
- Semantic results depend on one embedding model and one provider run; query vectors were cached per question text, document vectors per content digest in PostgreSQL.
- Offline unit tests use the hashing embedder: they check the harness and access rules, not retrieval quality.
- These are offline numbers only. Production relevance monitoring would need sampled human review and is not covered here.

## Reproduce

```sh
docker compose -p ra-retrieval-eval up -d --wait postgres      # throwaway database (set COMPOSE_POSTGRES_PORT and passwords)
alembic upgrade head
export EMBEDDING_PROVIDER=gemini               # plus the Gemini key; keyword-only needs none
python -m retail_analytics.bootstrap.retrieval_eval prepare      # executives + corpus (embeds each digest once)
python -m retail_analytics.bootstrap.retrieval_eval sweep --channels fused   # tuning split only
python -m retail_analytics.bootstrap.retrieval_eval benchmark    # runner result files in evaluation-results/retrieval/
python -m retail_analytics.bootstrap.retrieval_eval report --split heldout --by-category fused-tuned
python -m retail_analytics.bootstrap.retrieval_eval manifest     # regenerate manifest.json after editing labels.json
```

Set `RETRIEVAL_EVAL_DIR` to this folder when running from another directory. `prepare` publishes synthetic examples, so use a throwaway database. If the embedding provider is unavailable the semantic variants are reported as blocked, never replaced by another embedder. Result files use the standard runner format (one scenario per question, per-question metrics as measurements, access and no-match checks as expectations); `retail-analytics-eval run --manifest evaluation/retrieval/manifest.json --target package.module:factory --mode live --capability database` also works with any target that implements the runner port.

## T36-F1: defaults adopted (before and after)

Chosen on the tuning split only; held-out was reported, not used to pick anything. The benchmark was rerun from scratch for this change (fresh database, document vectors re-embedded once, cached query vectors reused); every T36 variant reproduced its T36 numbers exactly, so the "before" rows are unchanged measurements.

Tuning sweep (`sweep --channels fused`, now also over the semantic weight 1, 2, 3, 5, 10): similarity 0.70 with coverage 0.67 or 0.75 tied for the best objective (recall@3 0.935, precision@3 0.538, no-match 6/6, 0 false declines, 0 exposures; this sweep averages per question over the 31 expecting a match, so its recall differs slightly from the table). Weights 2 to 10 gave the same, higher tuning MRR (0.919) than weight 1 (0.903); weight 2 is the smallest weight that reaches it, so it was chosen (3 is no better on tuning). Ties still go to the stricter coverage (0.75), as in T36. The selection key now also uses MRR as the second criterion, after the objective.

| | before (T24 placeholders, equal-weight RRF) | after (T36-F1 defaults) |
|---|---|---|
| min similarity | 0.55 | 0.70 (gemini-embedding-2) |
| min lexical coverage | 0.5 | 0.75 |
| fusion | RRF k=60, weights 1:1 | weighted RRF k=60, semantic 2 : keyword 1 |
| config version | `rrf60-n10-k3-sim0.55-lex0.5` | `rrf60-n10-k3-sim0.7-lex0.75-ws2.0` |

Held-out (38 questions, 31 with a relevant example; 7 no-match; 28 expecting a match), same labels, same run:

| variant | P@3 | R@3 | MRR | nDCG@3 | no-match correct | false declines | access violations |
|---|---|---|---|---|---|---|---|
| before: fused-placeholder | 0.33 | 0.88 | 0.84 | 0.84 | 0/7 | 0/28 | 0 |
| after: fused-default | 0.65 (n=25) | 0.76 | 0.72 | 0.73 | 7/7 | 3/28 | 0 |

What the held-out numbers say, honestly:
- No-match handling went from 0/7 to 7/7 and precision doubled (0.33 to 0.65). The price is 3 of 28 false declines and lower recall (0.88 to 0.76) and MRR (0.84 to 0.72). That trade-off comes from the similarity threshold, as T36 found.
- The weighted fusion changes the result only where thresholds leave several candidates. With open thresholds it lifts fused ranking toward semantic-only: held-out R@3 0.69 to 0.82, MRR 0.67 to 0.72 (tuning R@3 0.86 to 0.93, MRR 0.79 to 0.85), but it still does not beat semantic-only open (R@3 0.91, MRR 0.90). With the shipped thresholds, fused-default and fused-tuned (equal weight) are identical on held-out, because the coverage threshold almost never admits a keyword-only candidate; the gain on tuning MRR is 0.85 to 0.86. So the weight is a safeguard that makes the semantic channel primary rather than a measured held-out improvement.
- Access violations: 0 in every variant on both splits.
- The semantic-only variant ranks as well as the fused one at these defaults; fusion is kept because the keyword channel costs nothing and could help exact-term questions, which this small benchmark cannot show. Revisit if the corpus grows.

The thresholds belong to `gemini-embedding-2` at 768 dimensions. The offline hashing embedder keeps 0.55 / 0.5 (unset settings resolve per provider in `bootstrap/retrieval.py`) because its cosine scale is different and it is not measured here; set the `RETRIEVAL_*` variables to override either.
