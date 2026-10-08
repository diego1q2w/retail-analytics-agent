"""Authorized hybrid Golden retrieval on a labeled miniature corpus."""

from __future__ import annotations

from dataclasses import replace

import pytest

from retail_analytics.adapters.embedding.hashing import HashingEmbedder
from retail_analytics.application.retrieval import (
    GoldenIndex,
    GoldenRetriever,
    RetrievalResult,
    RetrievalUnavailable,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.knowledge import (
    ApplicabilityContext,
    ErasureReason,
    GoldenVersion,
    KnowledgeAccess,
    MetricRef,
)
from retail_analytics.domain.retrieval import (
    RetrievalConfig,
    bm25,
    reciprocal_rank_fusion,
    tokenize,
)
from tests.knowledge_scenarios import (
    CONTEXT,
    SCOPE_ALL,
    SCOPE_NONE,
    Harness,
    draft,
    publish,
    submit,
)
from tests.unit.test_knowledge import harness as harness

CORPUS = {
    "revenue": (
        "How did monthly revenue trend over the last year?",
        "Aggregate completed item sales by month and compare complete months.",
    ),
    "returns": (
        "Which product categories have the highest return rates?",
        "Count returned items against shipped items per category.",
    ),
    "demographics": (
        "How does spending differ across customer age bands?",
        "Group completed sales by permitted age band and compare averages.",
    ),
    "state": (
        "Which states have the highest customer spending?",
        "Sum completed item sales by customer state and rank the states.",
    ),
}
RELAXED = RetrievalConfig(min_similarity=0.3, min_lexical_coverage=0.4)


def _draft(key: str, **kw: object):  # type: ignore[no-untyped-def]
    question, method = CORPUS[key]
    return replace(
        draft(question=question, **kw),  # type: ignore[arg-type]
        method_summary=method,
    )


class CountingEmbedder(HashingEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.embedded = 0

    async def embed_documents(self, texts):  # type: ignore[no-untyped-def]
        self.embedded += len(texts)
        return await super().embed_documents(texts)


class BrokenEmbedder(HashingEmbedder):
    async def embed_query(self, text):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider down")


def retriever(
    h: Harness,
    config: RetrievalConfig = RELAXED,
    embedder: HashingEmbedder | None = None,
) -> tuple[GoldenRetriever, GoldenIndex]:
    index = GoldenIndex(h.index_source, embedder or HashingEmbedder())
    return GoldenRetriever(index, h.reader, config), index


async def seed(h: Harness, *keys: str) -> dict[str, GoldenVersion]:
    out = {}
    for key in keys:
        out[key] = await publish(h, await submit(h, _draft(key)))
    return out


def ids(result: RetrievalResult) -> list[str]:
    return [e.question for e in result.examples]


@pytest.mark.asyncio
async def test_relevant_example_is_returned_first_with_provenance(
    harness: Harness,
) -> None:
    await seed(harness, *CORPUS)
    r, _ = retriever(harness)
    result = await r.retrieve(SCOPE_ALL, CONTEXT, "monthly revenue trend by month")
    assert result.examples
    assert result.examples[0].question == CORPUS["revenue"][0]
    top = result.hits[0]
    assert top.lexical_rank == 1 and top.semantic_rank == 1
    assert top.fused_score > 0 and result.eligible_count == 4
    assert result.embedding_model == "hashing-v1-256"
    assert result.config_version == RELAXED.version
    assert len(result.examples) <= 3
    again = await r.retrieve(SCOPE_ALL, CONTEXT, "monthly revenue trend by month")
    assert again.hits == result.hits  # reproducible


@pytest.mark.asyncio
async def test_no_match_returns_no_examples(harness: Harness) -> None:
    await seed(harness, *CORPUS)
    r, _ = retriever(harness, RetrievalConfig())
    for question in ("what is the weather forecast in Paris", "", "   "):
        result = await r.retrieve(SCOPE_ALL, CONTEXT, question)
        assert result.examples == () and result.hits == ()


@pytest.mark.asyncio
async def test_thresholds_and_candidate_counts_are_configurable(
    harness: Harness,
) -> None:
    await seed(harness, *CORPUS)
    question = "spending by state"
    loose, _ = retriever(
        harness,
        RetrievalConfig(max_results=3, min_similarity=-1, min_lexical_coverage=0.0),
    )
    assert len((await loose.retrieve(SCOPE_ALL, CONTEXT, question)).examples) == 3
    one, _ = retriever(
        harness,
        RetrievalConfig(max_results=1, min_similarity=-1, min_lexical_coverage=0.0),
    )
    assert len((await one.retrieve(SCOPE_ALL, CONTEXT, question)).examples) == 1
    strict, _ = retriever(
        harness, RetrievalConfig(min_similarity=0.99, min_lexical_coverage=1.0)
    )
    assert (
        await strict.retrieve(SCOPE_ALL, CONTEXT, question + " margin")
    ).examples == ()
    with pytest.raises(ValueError):
        RetrievalConfig(max_results=4)
    with pytest.raises(ValueError):
        RetrievalConfig(max_results=3, channel_candidates=2)


@pytest.mark.asyncio
async def test_wrong_scope_examples_never_surface(harness: Harness) -> None:
    secret = await publish(
        harness,
        await submit(
            harness,
            _draft("state", access=KnowledgeAccess.restricted(frozenset({"1", "2"}))),
        ),
    )
    r, _ = retriever(harness)
    outsider = ProductScope(frozenset({"9"}), 1)
    for scope in (outsider, SCOPE_NONE):
        result = await r.retrieve(scope, CONTEXT, CORPUS["state"][0])
        assert result.examples == () and result.hits == ()
        assert result.eligible_count == 0 and result.refused == ()
    allowed = await r.retrieve(SCOPE_ALL, CONTEXT, CORPUS["state"][0])
    assert [e.ref for e in allowed.examples] == [secret.ref]


@pytest.mark.asyncio
async def test_incompatible_versions_are_filtered_before_ranking(
    harness: Harness,
) -> None:
    other_schema = await publish(
        harness, await submit(harness, _draft("revenue", schema="store/2"))
    )
    other_metric = await publish(
        harness,
        await submit(
            harness, _draft("revenue", metrics=frozenset({MetricRef("revenue", 2)}))
        ),
    )
    r, _ = retriever(harness)
    result = await r.retrieve(SCOPE_ALL, CONTEXT, CORPUS["revenue"][0])
    assert result.examples == () and result.eligible_count == 0
    moved = ApplicabilityContext("store/2", {"revenue": 1})
    found = await r.retrieve(SCOPE_ALL, moved, CORPUS["revenue"][0])
    assert [e.ref for e in found.examples] == [other_schema.ref]
    assert other_metric.ref not in [e.ref for e in found.examples]


@pytest.mark.asyncio
async def test_stale_index_candidates_never_reach_the_model(
    harness: Harness,
) -> None:
    pubs = await seed(harness, "revenue", "returns", "state")
    _, index = retriever(harness)
    await index.sync()

    async def frozen() -> bool:  # simulate an index that missed every change
        return False

    index.sync = frozen  # type: ignore[method-assign]
    reviewer = harness.as_("reviewer")
    await harness.service.retire(reviewer, pubs["revenue"].ref, rationale="old")
    await harness.service.suspend(reviewer, pubs["returns"].ref, rationale="check")
    await harness.service.erase(
        reviewer, pubs["state"].ref, reason=ErasureReason.PRIVACY_REQUEST
    )
    question = "revenue returns states spending category month"
    cfg = RetrievalConfig(min_similarity=-1, min_lexical_coverage=0.0)
    stale, _ = retriever(harness, cfg)
    stale._index = index
    result = await stale.retrieve(SCOPE_ALL, CONTEXT, question)
    assert result.examples == ()
    assert {x.ref for x in result.refused} == {p.ref for p in pubs.values()}


@pytest.mark.asyncio
async def test_stale_entries_are_backfilled_with_next_ranked(
    harness: Harness,
) -> None:
    pubs = await seed(harness, "revenue", "returns", "state", "demographics")
    cfg = RetrievalConfig(max_results=2, min_similarity=-1, min_lexical_coverage=0.0)
    r, index = retriever(harness, cfg)
    await index.sync()
    first = await r.retrieve(SCOPE_ALL, CONTEXT, "monthly revenue trend")
    top = first.examples[0].ref
    index.sync = lambda: _noop()  # type: ignore[method-assign]
    await harness.service.retire(
        harness.as_("reviewer"), top, rationale="superseded elsewhere"
    )
    after = await r.retrieve(SCOPE_ALL, CONTEXT, "monthly revenue trend")
    assert top not in [e.ref for e in after.examples]
    assert len(after.examples) == 2
    assert any(x.ref == top for x in after.refused)
    assert pubs  # corpus unchanged otherwise


async def _noop() -> bool:
    return False


@pytest.mark.asyncio
async def test_reindex_follows_publication_retirement_and_new_versions(
    harness: Harness,
) -> None:
    embedder = CountingEmbedder()
    r, index = retriever(harness, embedder=embedder)
    assert (await r.retrieve(SCOPE_ALL, CONTEXT, CORPUS["revenue"][0])).examples == ()

    v1 = await publish(harness, await submit(harness, _draft("revenue")))
    found = await r.retrieve(SCOPE_ALL, CONTEXT, CORPUS["revenue"][0])
    assert [e.ref for e in found.examples] == [v1.ref]
    assert embedder.embedded == 1
    await r.retrieve(SCOPE_ALL, CONTEXT, CORPUS["revenue"][0])
    assert embedder.embedded == 1  # unchanged feed: no rebuild, no re-embedding

    v2 = await publish(
        harness,
        await submit(harness, _draft("revenue"), example_id=v1.example_id),
    )
    assert v2.version == 2
    found = await r.retrieve(SCOPE_ALL, CONTEXT, CORPUS["revenue"][0])
    assert [e.ref for e in found.examples] == [v2.ref]  # v1 superseded
    assert index.size == 1

    await harness.service.retire(harness.as_("reviewer"), v2.ref, rationale="obsolete")
    assert (await r.retrieve(SCOPE_ALL, CONTEXT, CORPUS["revenue"][0])).examples == ()
    assert index.size == 0


@pytest.mark.asyncio
async def test_provider_failure_is_reported_not_swallowed(
    harness: Harness,
) -> None:
    await seed(harness, "revenue")
    r, _ = retriever(harness, embedder=BrokenEmbedder())
    with pytest.raises(RetrievalUnavailable):
        await r.retrieve(SCOPE_ALL, CONTEXT, "revenue")


def test_fusion_ranks_by_rank_and_is_deterministic() -> None:
    fused = reciprocal_rank_fusion({"a": ["x", "y"], "b": ["y", "z"]}, 60)
    assert fused["y"] > fused["x"] > fused["z"]
    assert fused == reciprocal_rank_fusion({"b": ["y", "z"], "a": ["x", "y"]}, 60)


def test_bm25_uses_only_the_given_documents() -> None:
    docs = {"d1": tokenize("monthly revenue trend"), "d2": tokenize("return rates")}
    scores = bm25(tokenize("revenue trends"), docs)
    assert scores["d1"].coverage == 1.0 and scores["d1"].score > 0
    assert scores["d2"].score == 0.0
    assert bm25((), docs) == {} and bm25(("x",), {}) == {}


@pytest.mark.asyncio
async def test_hashing_embedder_is_deterministic_and_unit_length() -> None:
    e = HashingEmbedder(64)
    a, b = (
        await e.embed_query("monthly revenue"),
        await e.embed_query("monthly revenue"),
    )
    assert a == b and abs(sum(x * x for x in a) - 1.0) < 1e-9
    assert await e.embed_documents(["monthly revenue"]) == [a]
    assert await e.embed_query("the") == [0.0] * 64  # only stopwords


def test_bootstrap_builds_fixture_retriever_and_rejects_gemini_without_key() -> None:
    from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
    from retail_analytics.bootstrap.retrieval import build_embedder, retrieval_config

    fixture = load_backend_settings(environ={}, env_file=None)
    assert isinstance(build_embedder(fixture), HashingEmbedder)
    assert retrieval_config(fixture).max_results == 3
    gemini = load_backend_settings(
        environ={"RETAIL_ANALYTICS_EMBEDDING_PROVIDER": "gemini"}, env_file=None
    )
    with pytest.raises(ConfigError):
        build_embedder(gemini)
