# Release verification record

A bounded final audit of the deliverables in this repository, done on
2026-10-09 on the tree that follows commit `ee3e0a6` (after the real-model
evaluation and the architecture probe fix). It reuses the verification
records that already exist and adds
only the checks listed here. It is not a new test campaign.

**Passing these checks is not permission to publish.** Publishing the
repository needs a separate, explicit decision by the author, and two items
below (dataset terms, history) block a public release until they are
resolved.

## Summary

| Area | Result |
| --- | --- |
| Secrets and private material in tracked files and history | No findings |
| Raw results and personal data in tracked files | No findings |
| Frozen extract redistribution terms | **OPEN: blocks public release** |
| Truthful coverage in public documentation | Reconciled (see below) |
| Fixture-mode setup and chat on the final tree | Passed |
| Security release gates | Met ([security verification](../security-verification.md)) |
| Real-model evaluation | Measured ([evaluation/real-model](../../evaluation/real-model/README.md)) |
| Human reviews and sign-offs | **OPEN** (listed below) |
| Lint | Passed (`ruff check .`, `ruff format --check .`) |

## What was checked

### Secrets and private material

Run from the repository root over every tracked file (678 files) and, where
noted, every commit reachable from `HEAD` (77 commits).

| Check | Command (summary) | Result |
| --- | --- | --- |
| No environment files, credential files or keys tracked | `git ls-files` filtered for `.env`, `credentials*`, `service-account*`, `*.pem`, `*.key`, `evaluation-results/` | None. `src/retail_analytics/bootstrap/check_credentials.py` matched by name only; it is source code |
| Key patterns | `git grep` for Google API keys, OpenAI keys, private-key blocks, `"private_key"`, GitHub and Slack tokens | Only fake canaries in tests (`tests/unit/models/stubs.py`, `tests/unit/models/test_composition.py`, `tests/unit/telemetry/test_facade.py`, `tests/unit/test_config.py`), used to prove secrets are not printed |
| Key patterns in history | `git log -p --all -G<pattern>` | Only the same test canary |
| Private planning and source material, tracked or linked | `git ls-files` and `git grep` for the private planning and resource folders; `git log --all --name-only` | Never tracked in any commit; no links |
| Assignment text | Every line of the private assignment longer than 60 characters, searched as a fixed string in every tracked file and in all 77 commits | 0 matches |
| Local paths and personal contact details | `git grep` for home-directory paths and personal e-mail addresses | None |

### Raw results and personal data

- `evaluation-results/` (local run output) is ignored by `.gitignore` and
  not tracked.
- `evaluation/real-model/results/` holds released answers and identifiers
  only. The data behind it is the pseudonymized extract or the synthetic
  held-out fixture. The transcript of the request for names and e-mails
  (`ho-l1-pii-names-emails.md`) shows opaque `cus_…` references and age
  bands, no names or contact details. No e-mail address appears in any
  transcript.
- The frozen extract (`evaluation/realdata/extract/`, four gzip CSV files,
  784 KB) was inspected by content, not by column name:
  `orders(order_id, user_id, created_at)`,
  `order_items(id, order_id, user_id, product_id, status, sale_price)`,
  `users(id, age, state)` with age as a 5-year band start, and
  `products(id, name, category)` with catalogue product names. Identifiers
  are dense pseudonyms assigned inside BigQuery. No e-mail address occurs in
  the decompressed data. The provenance file
  (`extract-manifest.json`) holds BigQuery job IDs, table metadata and
  digests, and no project ID or local path.
- `python -m retail_analytics.bootstrap.realdata_benchmark verify` passed:
  "benchmark artifacts are intact and reproduce from the frozen extract"
  (digests, expected values and the privacy scan).

Pseudonymous references plus demographics are not a guarantee of
anonymity (see [known limitations](../architecture/known-limitations.md)).

### Frozen extract: redistribution terms (OPEN, blocks public release)

The extract is derived from the public `bigquery-public-data.thelook_ecommerce`
dataset. Public query access is not permission to redistribute. The
applicable dataset terms have **not** been confirmed by the author, and this
audit does not draw a legal conclusion.

Decision taken in this audit: the extract stays in the working tree, and
public release is **blocked** until the author confirms the terms. Removing
the files now would not make the repository publishable:

- The files are already in history (commit `59e79eb`). Deleting them in a
  later commit leaves them in every published clone.
- The real-data benchmark, its tests and the real-model evaluation read
  them. Removing them means changing those tests and tooling as well.
- Local setup does not depend on them. The fixture-mode setup below ran
  without reading the extract.

If the terms are confirmed and allow redistribution with attribution:
record the source URL, the date checked, the licence and the attribution
text in `evaluation/realdata/README.md`, then publish.

If redistribution is not allowed, or stays unconfirmed, the proposal for the
author's approval (nothing has been done):

1. Remove `evaluation/realdata/extract/*.csv.gz` in a commit and make the
   benchmark tests and the real-model runner skip with a clear message when
   the extract is missing. Keep `extract-manifest.json` so a regenerated
   extract can be compared by digest.
2. Document regeneration: `python -m retail_analytics.bootstrap.realdata_benchmark
   extract --refresh`, then `expected` and `manifest` (needs BigQuery
   access; the public source drifts, so a new extract is a new data
   version with new expected values).
3. Publish a history without those blobs: either a new repository with a
   single squashed initial commit of the final tree, or a rewritten copy
   (for example `git filter-repo --path evaluation/realdata/extract/ --invert-paths`
   on a fresh clone). Never rewrite this working repository in place.
4. Before pushing, verify that the published refs contain no extract blob:
   `git rev-list --objects --all | grep 'evaluation/realdata/extract/.*\.csv\.gz'`
   must print nothing.

### Truthful coverage

- Public documentation now links to the published results instead of
  "pending" markers: [requirements](../architecture/requirements.md) and
  [known limitations](../architecture/known-limitations.md) point to the
  real-model evaluation, the security verification and the recovery
  walkthrough, and say which parts are measured, done, pending or deferred.
- The README "Project status" section described an early skeleton. It now
  describes the working prototype and links the evidence and open items.
- Gemini attribution: live agent runs before the Gemini schema fix were
  answered by the GPT backup. The only public result that claims Gemini
  answers is the real-model evaluation, which verified attribution from the
  runtime's telemetry (Gemini answered all ten conversations). The
  limitations page now says that earlier live runs were GPT.
- Names: the documentation states that query results never contain person
  names (catalogue direct identifiers, compiler refusal, result-boundary
  re-check), and that the cue-based name detector is a second line of
  defence for typed text, so a name typed without a cue is echoed unmasked.
  The requirements page sentence that said "the model never sees names" was
  narrowed to query results.
- Deferred, not counted as implemented: the system-level learning loop
  automation, two-model judge calibration, multi-period evidence metadata,
  the report-title fix after access narrows, and an exhaustive fault matrix.

### Setup on the final tree (fixture mode)

A short smoke test, not a full clean-machine rehearsal (that was done for
portable setup earlier). Isolated Compose project `ra-test-t42`, free ports,
a separate environment file, telemetry off:

```sh
./scripts/bootstrap.sh --env-file <tmp>/smoke.env --project ra-test-t42 \
  --postgres-port 55462 --no-telemetry        # 9/9 steps, ~10 s
./scripts/dev.sh --env-file <tmp>/smoke.env --project ra-test-t42 --no-telemetry
curl http://127.0.0.1:18092/healthz           # mode fixture, execution local
retail-analytics-dev-access token local-admin > <tmp>/token
analytics status                              # backend ok (fixture, local)
analytics chat                                # one question
docker compose -p ra-test-t42 down -v
```

Results: bootstrap completed all steps (PostgreSQL healthy, migrations at
head, local admin and two demo executives provisioned, ten Golden seeds
published and embedded, configuration valid, credential check skipped
because none are configured). The API reported `mode=fixture`,
`execution_backend=local`. The chat created a session and returned the
expected fixture answer ("No language model is configured for this backend
(fixture mode)…"). The stack and its volume were removed afterwards. Live
mode (BigQuery and a model key) was not exercised here; the live save, read
and delete examples in the README remain unrehearsed.

### Existing verification records reused

- [Security verification](../security-verification.md): privacy, product
  authorization, malicious input and deletion confirmation, plus four
  release gates. All four gates are met.
- [Real-model evaluation](../../evaluation/real-model/README.md): ten
  conversations, 41/41 expected numbers in the released evidence, 40/41
  stated in the answer or report, 16/16 expected labels; strict runner
  checks 29/80 (they fail on output column names, not values);
  judge-scored dimensions unscored. Live data is a dated drift comparison
  only: on 2026-10-09 all seven reference queries differed from the frozen
  extract.
- [Recovery walkthrough](../recovery-walkthrough.md): recovery tests run in
  the repository checks; the human walkthrough is written but not yet run.
- [Retrieval benchmark](../../evaluation/retrieval/README.md): measured.
- Architecture probe (`tests/architecture`): reviewed independently. The
  review found a required fix of low severity: the probe's library
  exemption hid environment and file effects triggered through Pydantic
  callbacks (for example a `default_factory` partial) and exempted all
  installed packages. The exemption now covers four exact Pydantic effects. Fixed in T03-F1 (commit `ee3e0a6`).
  Import-layering checks were not affected.

## Open items for the author

None of these can be closed by an automated check.

| Item | Where | Status |
| --- | --- | --- |
| Human report review | [evaluation/real-model/human-review](../../evaluation/real-model/human-review/README.md) | Pending: no named reviewer yet |
| Reference-figure sign-offs: Golden seed semantics, held-out and real-data expected figures | [golden seeds](../golden-seeds.md), [evaluation/heldout](../../evaluation/heldout/README.md), [evaluation/realdata](../../evaluation/realdata/README.md) | Pending: reproduced by two routes, not signed off by a named reviewer (reviewer, date, version, digest) |
| Retrieval relevance labels | [evaluation/retrieval](../../evaluation/retrieval/README.md) | Written by the implementing agent alone; no second annotator (disclosed) |
| Human CLI walkthrough | [recovery walkthrough](../recovery-walkthrough.md) | Pending: result columns empty |
| Dataset redistribution terms for the frozen extract | this page, above | Pending: blocks public release |
| Published history free of the extract (if terms do not allow it) | this page, above | Proposal awaiting approval |
| Author's framework experience statement | [technology choices](../architecture/technology-choices.md) | Not yet provided by the author |
| Architecture probe fix | T03-F1 (`ee3e0a6`) | Fixed |
| Decision to publish | n/a | Not given. All checks passing is not permission to publish |
