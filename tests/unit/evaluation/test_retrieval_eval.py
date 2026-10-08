"""Retrieval benchmark: metrics, label soundness, corpus lifecycle, exposure.

The pipeline tests run the real retriever on an in-memory store with the
offline hashing embedder. That exercises the harness and the access rules; it is
not a measurement of model retrieval quality (the live run is).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from retail_analytics.adapters.embedding.hashing import HashingEmbedder
from retail_analytics.adapters.embedding.query_cache import CachedQueryEmbedder
from retail_analytics.adapters.evaluation.retrieval_files import (
    DATA_DIR,
    load_corpus,
    load_labels,
)
from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.application.artifacts import (
    ArtifactMaintenance,
    ArtifactPolicy,
    ArtifactService,
)
from retail_analytics.application.authorization import (
    AccessResolver,
    OwnershipGuard,
)
from retail_analytics.application.evaluation.retrieval_labels import (
    EvalCorpus,
    LabelSet,
    jaccard,
    validate_labels,
)
from retail_analytics.application.evaluation.retrieval_metrics import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from retail_analytics.application.evaluation.retrieval_report import (
    estimate,
    summarize,
)
from retail_analytics.application.evaluation.retrieval_target import (
    RetrievalEvalTarget,
    build_manifest,
    corpus_entries,
    is_eligible,
    load_eval_corpus,
    score_ranking,
)
from retail_analytics.application.evaluation.retrieval_tuning import best, sweep
from retail_analytics.application.evaluation.runner import RunConfig, run_manifest
from retail_analytics.application.golden_seed_library import (
    SEED_SCHEMA_VERSION,
    seed_library,
)
from retail_analytics.application.golden_seeding import seed_principals
from retail_analytics.application.knowledge import (
    GoldenKnowledgeReader,
    KnowledgeService,
)
from retail_analytics.application.retrieval import (
    GoldenIndex,
    GoldenRetriever,
    RetrievalResult,
)
from retail_analytics.domain.access import ProductScope, Role
from retail_analytics.domain.knowledge import ApplicabilityContext, ExampleRef
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.retrieval import (
    CHANNELS,
    LEXICAL,
    SEMANTIC,
    RetrievalConfig,
)
from tests.unit.memory_knowledge import MemoryKnowledgeRepository
from tests.unit.test_artifacts import MemoryCatalog
from tests.unit.test_knowledge import _Directory, _executive, _NoRecords

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 8, tzinfo=UTC)
METRICS = {
    m: default_catalog().latest_version(m) for m in default_catalog().metric_ids()
}


@pytest.fixture(scope="module")
def labels() -> LabelSet:
    return load_labels(ROOT / DATA_DIR / "labels.json")


@pytest.fixture(scope="module")
def corpus() -> EvalCorpus:
    return load_corpus(ROOT / DATA_DIR / "corpus.json")


# -- metrics: values computed by hand ----------------------------------------


def test_metrics_match_hand_computed_values() -> None:
    ranked = ["a", "x", "b"]
    relevant = frozenset({"a", "b", "c"})
    grades = {"a": 2, "b": 1, "c": 1}
    assert precision_at_k(ranked, relevant, 1) == 1.0
    assert precision_at_k(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert recall_at_k(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert recall_at_k(ranked, relevant, 1) == pytest.approx(1 / 3)
    assert reciprocal_rank(["x", "b"], relevant) == 0.5
    assert reciprocal_rank(["x", "y"], relevant) == 0.0
    # DCG = 3/log2(2) + 0 + 1/log2(4) = 3.5; ideal = 3 + 1/log2(3) + 1/log2(4).
    ideal = 3 + 1 / math.log2(3) + 0.5
    assert ndcg_at_k(ranked, grades, 3) == pytest.approx(3.5 / ideal)
    swapped = ndcg_at_k(["b", "a"], {"a": 2, "b": 1}, 2)
    assert swapped is not None and swapped < 1.0


def test_undefined_metrics_are_none_not_zero() -> None:
    assert precision_at_k([], frozenset({"a"}), 3) is None
    assert recall_at_k(["a"], frozenset(), 3) is None
    assert reciprocal_rank(["a"], frozenset()) is None
    assert ndcg_at_k(["a"], {}, 3) is None


def test_bootstrap_interval_is_reproducible_and_wide_for_small_n() -> None:
    values = [1.0, 0.0, 1.0, 1.0, 0.0]
    first, second = estimate(values), estimate(values)
    assert first == second
    assert first.n == 5 and first.mean == pytest.approx(0.6)
    assert first.low is not None and first.high is not None
    assert first.high - first.low > 0.4
    assert estimate([]).mean is None


# -- label soundness ---------------------------------------------------------


def test_labels_are_sound_and_cover_every_required_case(
    labels: LabelSet, corpus: EvalCorpus
) -> None:
    seeds = seed_library()
    problems = validate_labels(
        labels,
        frozenset(corpus_entries(corpus, seeds)),
        {e.key: e.question for e in corpus.examples},
        [s.question for s in seeds],
    )
    assert problems == []
    for split in ("tuning", "heldout"):
        categories = {q.category for q in labels.split(split)}
        assert {
            "paraphrase" if split == "tuning" else "direct",
            "ambiguous",
            "incompatible",
            "unauthorized",
            "no_match",
            "retired",
        } <= categories
    assert len(labels.split("heldout")) >= 30 and len(labels.split("tuning")) >= 30


def test_validator_catches_leakage_and_split_spanning(
    labels: LabelSet, corpus: EvalCorpus
) -> None:
    keys = frozenset(corpus_entries(corpus))
    tuning = labels.split("tuning")[0]
    held = next(q for q in labels.questions if q.split == "heldout")
    leaked = held.model_copy(update={"text": tuning.text})
    swapped = tuple(leaked if q.id == held.id else q for q in labels.questions)
    problems = validate_labels(
        labels.model_copy(update={"questions": swapped}), keys, {}, []
    )
    assert any(p.startswith("leakage") for p in problems)
    spanning = tuning.model_copy(update={"split": "heldout", "id": "rt-x"})
    problems = validate_labels(
        labels.model_copy(update={"questions": (*labels.questions, spanning)}),
        keys,
        {},
        [],
    )
    assert any("spans splits" in p for p in problems)
    seed_copy = held.model_copy(update={"text": seed_library()[0].question})
    problems = validate_labels(
        labels.model_copy(update={"questions": (*labels.questions, seed_copy)}),
        keys,
        {},
        [seed_library()[0].question],
    )
    assert any("repeats a corpus question" in p for p in problems)
    assert jaccard("revenue by month", "month revenue") == 1.0


def test_committed_manifest_matches_labels(labels: LabelSet) -> None:
    committed = json.loads((ROOT / DATA_DIR / "manifest.json").read_text())
    assert committed == json.loads(build_manifest(labels).model_dump_json()), (
        "run: python -m retail_analytics.bootstrap.retrieval_eval manifest"
    )
    assert len(committed["scenarios"]) == len(labels.questions)


def test_independent_eligibility_rules(corpus: EvalCorpus) -> None:
    entries = corpus_entries(corpus)
    women = frozenset(str(i) for i in range(1, 21))
    men = frozenset(str(i) for i in range(15990, 16010))

    def eligible(key: str, scope: frozenset[str]) -> bool:
        return is_eligible(entries[key], scope, SEED_SCHEMA_VERSION, METRICS)

    assert eligible("monthly-revenue-trend", frozenset())
    assert not eligible("x-men-top-products", women)
    assert eligible("x-men-top-products", men)
    assert not eligible("x-women-category-mix", men)
    assert not eligible("x-revenue-v2-monthly", women | men)
    assert not eligible("x-schema2-top-products", women | men)
    assert not eligible("x-retired-quarterly-revenue", women | men)


# -- exposure scoring (fault injection) --------------------------------------


def test_scoring_flags_exposure_and_counts_only_authorized_relevant(
    labels: LabelSet, corpus: EvalCorpus
) -> None:
    entries = corpus_entries(corpus)
    question = next(q for q in labels.questions if q.id == "rt-t26")  # women, men's top
    assert question.scope == "women" and "x-men-top-products" in question.forbidden
    women = frozenset(labels.scopes["women"])
    clean = score_ranking(
        question,
        ["top-products-by-revenue"],
        entries,
        women,
        SEED_SCHEMA_VERSION,
        METRICS,
    )
    assert clean.measurements["access_violations"] == 0
    assert clean.measurements["recall_at_3"] == 1.0
    leaky = score_ranking(
        question,
        ["x-men-top-products", "top-products-by-revenue"],
        entries,
        women,
        SEED_SCHEMA_VERSION,
        METRICS,
    )
    assert leaky.measurements["access_violations"] == 1
    for key in ("x-revenue-v2-monthly", "x-retired-quarterly-revenue"):
        bad = score_ranking(
            question, [key], entries, women, SEED_SCHEMA_VERSION, METRICS
        )
        assert bad.measurements["access_violations"] == 1


# -- pipeline on an in-memory store ------------------------------------------


class Pipeline:
    def __init__(
        self,
        service: KnowledgeService,
        reader: GoldenKnowledgeReader,
        repo: MemoryKnowledgeRepository,
    ) -> None:
        self.service, self.reader, self.repo = service, reader, repo

    def retriever(
        self, config: RetrievalConfig, embedder: HashingEmbedder | None = None
    ) -> GoldenRetriever:
        index = GoldenIndex(self.repo, embedder or HashingEmbedder())
        return GoldenRetriever(index, self.reader, config)


async def build_pipeline(
    tmp_path: Path, labels: LabelSet, corpus: EvalCorpus
) -> Pipeline:
    products = {p for scope in labels.scopes.values() for p in scope}
    people = {
        "eval-author": _executive("eval-author", {Role.EXECUTIVE}, products),
        "eval-reviewer": _executive("eval-reviewer", {Role.REVIEWER}, products),
    }
    resolver = AccessResolver(
        _Directory(people),
        OwnershipGuard(_NoRecords(), _NoRecords(), _NoRecords()),
    )
    blobs = LocalBlobStore(tmp_path / "artifacts")
    catalog = MemoryCatalog()
    artifacts = ArtifactService(catalog, blobs, ArtifactPolicy())
    repo = MemoryKnowledgeRepository()
    service = KnowledgeService(
        resolver,
        repo,
        artifacts,
        ArtifactMaintenance(catalog, blobs),
        clock=lambda: NOW,
    )
    author, reviewer = seed_principals("eval-author", "eval-reviewer")
    await load_eval_corpus(service, author, reviewer, corpus)
    return Pipeline(service, GoldenKnowledgeReader(repo, artifacts), repo)


@pytest.mark.asyncio
async def test_corpus_publishes_and_the_retired_example_is_not_indexed(
    tmp_path: Path, labels: LabelSet, corpus: EvalCorpus
) -> None:
    pipeline = await build_pipeline(tmp_path, labels, corpus)
    again = await build_pipeline(tmp_path / "again", labels, corpus)
    assert again.repo is not pipeline.repo
    documents = await pipeline.repo.published_documents(None, 100)
    published = {d.ref.example_id for d in documents}
    entries = corpus_entries(corpus)
    expected = {e.example_id for e in entries.values() if e.status == "published"}
    assert published == expected
    assert len(documents) == len(seed_library()) + len(corpus.examples) - 1
    retired = entries["x-retired-quarterly-revenue"].example_id
    assert retired not in published


def make_target(
    pipeline: Pipeline,
    labels: LabelSet,
    corpus: EvalCorpus,
    config: RetrievalConfig,
    name: str,
) -> RetrievalEvalTarget:
    return RetrievalEvalTarget(
        f"retrieval:{name}",
        pipeline.retriever(config).retrieve,
        labels,
        corpus_entries(corpus),
        METRICS,
    )


VARIANT_CONFIGS = {
    "keyword": RetrievalConfig(
        channels=frozenset({LEXICAL}), min_similarity=-1.0, min_lexical_coverage=0.0
    ),
    "semantic": RetrievalConfig(
        channels=frozenset({SEMANTIC}), min_similarity=-1.0, min_lexical_coverage=0.0
    ),
    "fused": RetrievalConfig(
        channels=CHANNELS, min_similarity=-1.0, min_lexical_coverage=0.0
    ),
    "fused-gated": RetrievalConfig(),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(VARIANT_CONFIGS))
async def test_every_variant_runs_through_the_runner_with_zero_exposure(
    name: str, tmp_path: Path, labels: LabelSet, corpus: EvalCorpus
) -> None:
    pipeline = await build_pipeline(tmp_path, labels, corpus)
    target = make_target(pipeline, labels, corpus, VARIANT_CONFIGS[name], name)
    result = await _run(target, labels)
    assert not [c for c in result.cases if c.status in ("blocked", "errored")]
    assert result.aggregates.safety_gate_failures.numerator == 0
    summary = summarize(name, result, labels, "heldout")
    assert summary.access_violations == 0
    assert summary.questions == len(labels.split("heldout"))
    assert summary.no_match_correct[1] == sum(
        q.expect == "none" for q in labels.split("heldout")
    )
    tuning = summarize(name, result, labels, "tuning")
    assert tuning.access_violations == 0


async def _run(target: RetrievalEvalTarget, labels: LabelSet):  # type: ignore[no-untyped-def]
    import asyncio

    config = RunConfig(
        mode="live",
        available_capabilities=frozenset({"database"}),
        versions={"retrieval": "test"},
    )

    def work():  # type: ignore[no-untyped-def]
        try:
            return run_manifest(build_manifest(labels), target, config)
        finally:
            target.close()  # the runner's loop lives on this worker thread

    return await asyncio.to_thread(work)


@pytest.mark.asyncio
async def test_single_channel_variants_use_only_their_channel(
    tmp_path: Path, labels: LabelSet, corpus: EvalCorpus
) -> None:
    pipeline = await build_pipeline(tmp_path, labels, corpus)
    scope = ProductScope(frozenset(labels.scopes["all"]), 1)
    ctx = ApplicabilityContext(SEED_SCHEMA_VERSION, METRICS)
    question = "month by month revenue for the last three months"
    keyword = await pipeline.retriever(VARIANT_CONFIGS["keyword"]).retrieve(
        scope, ctx, question
    )
    semantic = await pipeline.retriever(VARIANT_CONFIGS["semantic"]).retrieve(
        scope, ctx, question
    )
    assert keyword.hits and all(h.semantic_rank is None for h in keyword.hits)
    assert semantic.hits and all(h.lexical_rank is None for h in semantic.hits)
    assert "only-lexical" in VARIANT_CONFIGS["keyword"].version
    assert "only" not in RetrievalConfig().version
    with pytest.raises(ValueError):
        RetrievalConfig(channels=frozenset())
    with pytest.raises(ValueError):
        RetrievalConfig(channels=frozenset({"other"}))


class _FixedRetriever:
    """Test double that returns chosen example references (fault injection)."""

    def __init__(self, pipeline: Pipeline, example_ids: list[str]) -> None:
        self._pipeline = pipeline
        self._ids = example_ids

    async def retrieve(
        self, scope: ProductScope, context: ApplicabilityContext, question: str
    ) -> RetrievalResult:
        examples = []
        for example_id in self._ids:
            version = await self._pipeline.repo.get(ExampleRef(example_id, 1))
            assert version is not None and version.content is not None
            examples.append(
                _example(version.ref, version.content.question, version.content_digest)
            )
        return RetrievalResult(tuple(examples), (), (), 0, "fixed", "fixed")


def _example(ref: ExampleRef, question: str, digest: str | None):  # type: ignore[no-untyped-def]
    from retail_analytics.application.knowledge import GoldenExample
    from retail_analytics.domain.knowledge import Applicability, Origin

    return GoldenExample(
        ref,
        digest or "d",
        Origin.PROJECT_AUTHORED,
        question,
        "",
        "",
        "",
        Applicability("x"),
        True,
    )


@pytest.mark.asyncio
async def test_a_leaking_retriever_fails_the_security_gate(
    tmp_path: Path, labels: LabelSet, corpus: EvalCorpus
) -> None:
    pipeline = await build_pipeline(tmp_path, labels, corpus)
    entries = corpus_entries(corpus)
    leak = _FixedRetriever(pipeline, [entries["x-men-top-products"].example_id])
    target = RetrievalEvalTarget(
        "retrieval:leaky", leak.retrieve, labels, entries, METRICS
    )
    result = await _run(target, labels)
    failed = {c.scenario_id for c in result.cases if c.status == "failed"}
    women_questions = {q.id for q in labels.questions if q.scope == "women"}
    assert women_questions <= failed
    assert result.verdict == "failed"
    assert result.aggregates.safety_gate_failures.numerator > 0


@pytest.mark.asyncio
async def test_unavailable_embeddings_block_instead_of_passing(
    tmp_path: Path, labels: LabelSet, corpus: EvalCorpus
) -> None:
    pipeline = await build_pipeline(tmp_path, labels, corpus)

    class Down(HashingEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            raise RuntimeError("provider down")

    target = RetrievalEvalTarget(
        "retrieval:down",
        pipeline.retriever(RetrievalConfig(), Down()).retrieve,
        labels,
        corpus_entries(corpus),
        METRICS,
    )
    result = await _run(target, labels)
    assert result.verdict == "incomplete"
    assert {c.status for c in result.cases} == {"blocked"}
    assert summarize("down", result, labels, "heldout").questions == 0


@pytest.mark.asyncio
async def test_sweep_scores_settings_on_tuning_split_only(
    tmp_path: Path, labels: LabelSet, corpus: EvalCorpus
) -> None:
    pipeline = await build_pipeline(tmp_path, labels, corpus)
    entries = corpus_entries(corpus)
    seen: list[str] = []

    def make(config: RetrievalConfig) -> Callable[..., object]:
        inner = pipeline.retriever(config).retrieve

        async def retrieve(scope, context, text):  # type: ignore[no-untyped-def]
            seen.append(text)
            return await inner(scope, context, text)

        return retrieve

    configs = [
        RetrievalConfig(min_similarity=-1.0, min_lexical_coverage=0.0),
        RetrievalConfig(min_similarity=0.9, min_lexical_coverage=1.0),
    ]
    rows = await sweep(
        make,  # type: ignore[arg-type]
        configs,
        labels.split("tuning"),
        {k: frozenset(v) for k, v in labels.scopes.items()},
        entries,
        {e.example_id: k for k, e in entries.items()},
        ApplicabilityContext(SEED_SCHEMA_VERSION, METRICS),
    )
    held_texts = {q.text for q in labels.split("heldout")}
    assert not held_texts & set(seen)
    open_row, strict_row = rows
    assert open_row.recall_at_3 > strict_row.recall_at_3
    assert strict_row.no_match_rate >= open_row.no_match_rate
    assert best(rows).exposures == 0


@pytest.mark.asyncio
async def test_query_embedding_cache_embeds_each_question_once(tmp_path: Path) -> None:
    class Counting(HashingEmbedder):
        calls = 0

        async def embed_query(self, text: str) -> list[float]:
            Counting.calls += 1
            return await super().embed_query(text)

    path = tmp_path / "q.json"
    cache = CachedQueryEmbedder(Counting(), path)
    first = await cache.embed_query("monthly revenue")
    again = await cache.embed_query("monthly revenue")
    reloaded = CachedQueryEmbedder(Counting(), path)
    third = await reloaded.embed_query("monthly revenue")
    assert first == again == third and Counting.calls == 1
    assert "monthly" not in path.read_text()  # vectors only, no question text
