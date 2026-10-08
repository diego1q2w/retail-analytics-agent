"""Evaluation target and manifest for Golden retrieval.

The target runs one labeled question through a retriever and reports only
example keys, counts and metric values. It holds the labels (the harness), not
the retriever (the system under test), and judges exposure from the corpus
metadata, never from anything the retriever reports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from retail_analytics.application.authorization import Principal
from retail_analytics.application.evaluation.manifest import (
    ExactExpectation,
    Expectation,
    Manifest,
    Mode,
    Scenario,
    ScopeSpec,
    Turn,
)
from retail_analytics.application.evaluation.ports import (
    ScenarioInput,
    TargetObservation,
    TargetUnavailable,
)
from retail_analytics.application.evaluation.retrieval_labels import (
    EvalCorpus,
    EvalExample,
    LabeledQuestion,
    LabelSet,
)
from retail_analytics.application.evaluation.retrieval_metrics import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    relevant_set,
)
from retail_analytics.application.golden_seed_library import (
    SEED_SCHEMA_VERSION,
    SeedExample,
    seed_library,
)
from retail_analytics.application.golden_seeding import (
    seed_golden_library,
)
from retail_analytics.application.knowledge import (
    ApprovalChecks,
    ExampleDraft,
    KnowledgeService,
)
from retail_analytics.application.retrieval import (
    RetrievalResult,
    RetrievalUnavailable,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.knowledge import (
    Applicability,
    ApplicabilityContext,
    ExampleRef,
    KnowledgeAccess,
    MetricRef,
    Origin,
    Provenance,
    ReviewStatus,
    SourceKind,
)

KS = (1, 3)
MANIFEST_ID = "retrieval-golden-eval"
Retrieve = Callable[
    [ProductScope, ApplicabilityContext, str], Awaitable[RetrievalResult]
]


@dataclass(frozen=True, slots=True)
class CorpusEntry:
    """Ground-truth metadata of one corpus example (not read from the index)."""

    key: str
    example_id: str
    restricted_products: frozenset[str]
    schema_version: str
    metrics: Mapping[str, int]
    status: str


def corpus_entries(
    corpus: EvalCorpus, seeds: Sequence[SeedExample] | None = None
) -> dict[str, CorpusEntry]:
    entries: dict[str, CorpusEntry] = {}
    for seed in seeds if seeds is not None else seed_library():
        entries[seed.key] = CorpusEntry(
            seed.key,
            seed.example_id,
            frozenset(),
            SEED_SCHEMA_VERSION,
            {m.metric_id: m.version for m in seed.metrics},
            "published",
        )
    for ex in corpus.examples:
        entries[ex.key] = CorpusEntry(
            ex.key,
            ex.example_id,
            frozenset(ex.restricted_products),
            ex.schema_version,
            dict(ex.metrics),
            ex.final_status,
        )
    return entries


def is_eligible(
    entry: CorpusEntry,
    scope: frozenset[str],
    schema_version: str,
    metrics: Mapping[str, int],
) -> bool:
    """Independent restatement of the delivery rules, from corpus metadata."""
    return (
        entry.status == "published"
        and entry.restricted_products <= scope
        and entry.schema_version == schema_version
        and all(metrics.get(m) == v for m, v in entry.metrics.items())
    )


def _draft(example: EvalExample) -> ExampleDraft:
    restricted = frozenset(example.restricted_products)
    return ExampleDraft(
        question=example.question,
        sql=example.sql,
        method_summary=example.method_summary,
        report_markdown=example.report_markdown,
        applicability=Applicability(
            example.schema_version,
            frozenset(MetricRef(m, v) for m, v in example.metrics),
        ),
        access=KnowledgeAccess.restricted(restricted)
        if restricted
        else KnowledgeAccess.shared(),
        origin=Origin.PROJECT_AUTHORED,
        provenance=Provenance(SourceKind.AUTHORED),
        sanitization_attested=True,
    )


async def load_eval_corpus(
    service: KnowledgeService,
    author: Principal,
    reviewer: Principal,
    corpus: EvalCorpus,
) -> None:
    """Publish the seeds and the evaluation-only examples; safe to rerun.

    The author needs every restricted product in scope and the reviewer needs
    the same to judge restricted content.
    """
    await seed_golden_library(service, author, reviewer)
    for example in corpus.examples:
        version = await service.submit_candidate(
            author,
            _draft(example),
            idempotency_key=f"eval-{example.key}-r{corpus.revision}",
            example_id=example.example_id,
        )
        if version.status is ReviewStatus.CANDIDATE:
            await service.approve(
                reviewer,
                version.ref,
                rationale="Evaluation-only synthetic example, approved for the "
                "retrieval benchmark corpus.",
                checks=ApprovalChecks(correct=True, sanitized=True, applicable=True),
            )
            if example.final_status == "retired":
                await service.retire(
                    reviewer,
                    version.ref,
                    rationale="Retired on purpose: tests that retired content "
                    "is never retrieved.",
                )


class RetrievalEvalTarget:
    """Runs scenarios through ``retrieve`` on one persistent event loop."""

    def __init__(
        self,
        target_id: str,
        retrieve: Retrieve,
        labels: LabelSet,
        entries: Mapping[str, CorpusEntry],
        metric_versions: Mapping[str, int],
    ) -> None:
        self.target_id = target_id
        self._retrieve = retrieve
        self._questions = {q.id: q for q in labels.questions}
        self._labels = labels
        self._entries = dict(entries)
        self._by_example = {e.example_id: k for k, e in entries.items()}
        self._metric_versions = dict(metric_versions)
        self._runner = asyncio.Runner()
        self.trace: dict[str, tuple[str, ...]] = {}

    def close(self) -> None:
        self._runner.close()

    def _key(self, ref: ExampleRef) -> str:
        return self._by_example.get(ref.example_id, f"unknown:{ref.example_id}")

    async def _retrieve_once(
        self, scope: frozenset[str], context: ApplicabilityContext, text: str
    ) -> RetrievalResult:
        return await self._retrieve(ProductScope(scope, 1), context, text)

    def run(self, case: ScenarioInput) -> TargetObservation:
        question = self._questions[case.scenario_id]
        scope_ids = frozenset(case.scope.product_scope)
        context = ApplicabilityContext(
            self._labels.context.schema_version, self._metric_versions
        )
        try:
            result = self._runner.run(
                self._retrieve_once(scope_ids, context, case.dialogue[0].text)
            )
        except RetrievalUnavailable:
            raise TargetUnavailable from None
        ranked = tuple(self._key(e.ref) for e in result.examples)
        self.trace[question.id] = ranked
        return score_ranking(
            question,
            ranked,
            self._entries,
            scope_ids,
            self._labels.context.schema_version,
            self._metric_versions,
        )


def score_ranking(
    question: LabeledQuestion,
    ranked: Sequence[str],
    entries: Mapping[str, CorpusEntry],
    scope: frozenset[str],
    schema_version: str,
    metric_versions: Mapping[str, int],
) -> TargetObservation:
    eligible = {
        key
        for key, entry in entries.items()
        if is_eligible(entry, scope, schema_version, metric_versions)
    }
    violations = sum(
        1 for key in ranked if key not in eligible or key in question.forbidden
    )
    grades = {k: g for k, g in question.relevant.items() if k in eligible}
    relevant = relevant_set(grades)
    measurements: dict[str, float] = {
        "access_violations": float(violations),
        "returned_count": float(len(ranked)),
    }
    for k in KS:
        for name, value in (
            # Precision needs something relevant to find; a question with no
            # authorized relevant example is scored as no-match behavior only.
            (
                f"precision_at_{k}",
                precision_at_k(ranked, relevant, k) if relevant else None,
            ),
            (f"recall_at_{k}", recall_at_k(ranked, relevant, k)),
            (f"ndcg_at_{k}", ndcg_at_k(ranked, grades, k)),
        ):
            if value is not None:
                measurements[name] = value
    rr = reciprocal_rank(ranked, relevant)
    if rr is not None:
        measurements["reciprocal_rank"] = rr
    return TargetObservation(
        values={
            "access_violations": violations,
            "returned_count": len(ranked),
        },
        tool_calls=(),
        measurements=measurements,
    )


_GATED = frozenset({"unauthorized", "incompatible", "retired", "authorized_restricted"})


def build_manifest(
    labels: LabelSet, *, mode: Mode = "live", version: str | None = None
) -> Manifest:
    scenarios: list[Scenario] = []
    for q in labels.questions:
        expectations: list[Expectation] = [
            ExactExpectation(name="access_violations", expected=0)
        ]
        # Security cases are gated on exposure alone; whether they also decline
        # is scored as no-match behavior from the returned count.
        if q.expect == "none" and q.category == "no_match":
            expectations.append(ExactExpectation(name="returned_count", expected=0))
        scenarios.append(
            Scenario(
                id=q.id,
                title=f"{q.category}: {q.group}",
                level=1,
                category=q.category,
                mode=mode,
                importance="gate" if q.category in _GATED else "threshold",
                verification=("deterministic",),
                scope=ScopeSpec(
                    executive_ref=f"scope-{q.scope}",
                    product_scope=labels.scopes[q.scope],
                ),
                dialogue=(Turn(text=q.text),),
                expectations=tuple(expectations),
                requires=("database",) if mode == "live" else (),
                tags=(q.split, q.category, f"expect-{q.expect}"),
            )
        )
    return Manifest(
        manifest_id=MANIFEST_ID,
        manifest_version=version or labels.labels_version,
        scenarios=tuple(scenarios),
    )
