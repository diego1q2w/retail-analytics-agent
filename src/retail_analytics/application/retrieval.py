"""Authorized hybrid retrieval of Golden Knowledge examples.

Order of operations, each step narrowing what can leak:

1. Eligibility prefilter on the *index entries* (access scope and schema/metric
   applicability) before any scoring, so ranks, statistics and thresholds only
   ever involve examples this run may receive.
2. Keyword (BM25) and vector channels over the eligible set; absolute
   thresholds decide relevance, reciprocal rank fusion decides order.
3. Delivery through ``GoldenKnowledgeReader`` only, which rechecks status,
   access, compatibility and the pinned content digest, so a stale index
   entry (retired, suspended, erased, changed) never reaches the model.

The index is derived data: it holds the question plus reviewed method summary
(never SQL or reports), the access policy and the content digest.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from retail_analytics.application.knowledge import (
    GoldenExample,
    GoldenKnowledgeReader,
    IndexDocument,
    KnowledgeIndexSource,
    Refused,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.knowledge import ApplicabilityContext, ExampleRef
from retail_analytics.domain.retrieval import (
    LEXICAL,
    SEMANTIC,
    RetrievalConfig,
    bm25,
    cosine,
    rank,
    reciprocal_rank_fusion,
    tokenize,
)

_PAGE = 200


class TextEmbedder(Protocol):
    """Narrow embedding port. Vectors must be deterministic per (model, text)."""

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class EmbeddingStore(Protocol):
    """Durable vector cache keyed by (content digest, model id, dimensions).

    Holds vectors only: no text, identifiers or access policy.
    """

    async def load(
        self, digests: Sequence[str], model_id: str, dimensions: int
    ) -> dict[str, list[float]]: ...

    async def save(
        self, vectors: Mapping[str, Sequence[float]], model_id: str, dimensions: int
    ) -> None:
        """Idempotent: an existing key is left unchanged."""
        ...


class RetrievalUnavailable(Exception):
    """A channel failed (for example the embedding provider); no examples."""


@dataclass(frozen=True, slots=True)
class _Entry:
    document: IndexDocument
    tokens: tuple[str, ...]
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """Why an example was selected; carries no content."""

    ref: ExampleRef
    content_digest: str
    fused_score: float
    lexical_rank: int | None
    lexical_coverage: float
    semantic_rank: int | None
    semantic_similarity: float


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    examples: tuple[GoldenExample, ...]
    hits: tuple[RetrievalHit, ...]  # same order as ``examples``
    refused: tuple[Refused, ...]  # stale or ineligible at delivery time
    eligible_count: int
    embedding_model: str
    config_version: str


class GoldenIndex:
    """In-process lexical + vector index rebuilt from the trusted index feed.

    The corpus is tens of reviewed examples, so brute-force cosine is exact and
    needs neither a vector extension nor an external service. Vectors are
    cached by content digest and embedding model, so reindexing only embeds
    changed content.
    """

    def __init__(
        self,
        source: KnowledgeIndexSource,
        embedder: TextEmbedder,
        store: EmbeddingStore | None = None,
    ) -> None:
        self._source = source
        self._embedder = embedder
        self._store = store
        self._sequence = -1
        self._entries: dict[ExampleRef, _Entry] = {}
        self._vectors: dict[tuple[str, str], tuple[float, ...]] = {}

    @property
    def embedder(self) -> TextEmbedder:
        return self._embedder

    @property
    def size(self) -> int:
        return len(self._entries)

    async def sync(self) -> bool:
        """Reindex if the invalidation feed moved; True when rebuilt."""
        head = await self._head()
        if head == self._sequence:
            return False
        # Read the head first: a change landing mid-rebuild is applied on the
        # next sync rather than lost.
        documents = await self._published()
        await self._rebuild(documents)
        self._sequence = head
        return True

    async def _head(self) -> int:
        sequence = max(self._sequence, 0)
        while changes := await self._source.changes_after(sequence, _PAGE):
            sequence = changes[-1].sequence
        return sequence

    async def _published(self) -> list[IndexDocument]:
        documents: list[IndexDocument] = []
        after: ExampleRef | None = None
        while True:
            page = await self._source.published_documents(after, _PAGE)
            documents.extend(page)
            if len(page) < _PAGE or page[-1].ref == after:
                return documents
            after = page[-1].ref

    async def _rebuild(self, documents: Sequence[IndexDocument]) -> None:
        model = self._embedder.model_id
        missing = [
            d for d in documents if (d.content_digest, model) not in self._vectors
        ]
        if missing and self._store is not None:
            stored = await self._load(
                sorted({d.content_digest for d in missing}), model
            )
            for digest, vector in stored.items():
                self._vectors[(digest, model)] = tuple(vector)
            missing = [
                d for d in missing if (d.content_digest, model) not in self._vectors
            ]
        if missing:
            # Equal digests mean equal content: embed each digest once.
            unique = list({d.content_digest: d for d in missing}.values())
            vectors = await self._embed([_text(d) for d in unique])
            for doc, vector in zip(unique, vectors, strict=True):
                self._vectors[(doc.content_digest, model)] = tuple(vector)
            await self._persist(
                {d.content_digest: v for d, v in zip(unique, vectors, strict=True)},
                model,
            )
        live = {(d.content_digest, model) for d in documents}
        self._vectors = {k: v for k, v in self._vectors.items() if k in live}
        self._entries = {
            d.ref: _Entry(
                d, tokenize(_text(d)), self._vectors[(d.content_digest, model)]
            )
            for d in documents
        }

    async def _load(self, digests: list[str], model: str) -> dict[str, list[float]]:
        assert self._store is not None  # noqa: S101
        dimensions = self._embedder.dimensions
        try:
            found = await self._store.load(digests, model, dimensions)
        except Exception as error:
            raise RetrievalUnavailable("embedding store unavailable") from error
        return {d: v for d, v in found.items() if len(v) == dimensions}

    async def _persist(
        self, vectors: Mapping[str, Sequence[float]], model: str
    ) -> None:
        if self._store is None:
            return
        try:
            await self._store.save(vectors, model, self._embedder.dimensions)
        except Exception as error:
            raise RetrievalUnavailable("embedding store unavailable") from error

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        try:
            vectors = await self._embedder.embed_documents(texts)
        except Exception as error:
            raise RetrievalUnavailable("embedding failed") from error
        if len(vectors) != len(texts):
            raise RetrievalUnavailable("embedding provider returned wrong count")
        return vectors

    def eligible(
        self, scope: ProductScope, context: ApplicabilityContext
    ) -> list[_Entry]:
        return [
            e
            for e in self._entries.values()
            if e.document.access.permits(scope)
            and e.document.applicability.applies_to(context)
        ]


def _text(document: IndexDocument) -> str:
    return f"{document.question}\n{document.method_summary}"


class GoldenRetriever:
    def __init__(
        self,
        index: GoldenIndex,
        reader: GoldenKnowledgeReader,
        config: RetrievalConfig | None = None,
    ) -> None:
        self._index = index
        self._reader = reader
        self._config = config or RetrievalConfig()

    async def retrieve(
        self, scope: ProductScope, context: ApplicabilityContext, question: str
    ) -> RetrievalResult:
        """Up to ``max_results`` relevant examples; none when nothing is."""
        cfg = self._config
        await self._index.sync()
        eligible = self._index.eligible(scope, context)
        model = self._index.embedder.model_id
        empty = RetrievalResult((), (), (), len(eligible), model, cfg.version)
        query_tokens = tokenize(question)
        if not eligible or not (question.strip()):
            return empty
        try:
            query_vector = await self._index.embedder.embed_query(question)
        except Exception as error:
            raise RetrievalUnavailable("query embedding failed") from error
        hits = self._rank(eligible, query_tokens, query_vector)
        if not hits:
            return empty
        examples, ordered, refused = await self._deliver(scope, context, hits)
        return RetrievalResult(
            tuple(examples),
            tuple(ordered),
            tuple(refused),
            len(eligible),
            model,
            cfg.version,
        )

    def _rank(
        self,
        eligible: Sequence[_Entry],
        query_tokens: Sequence[str],
        query_vector: Sequence[float],
    ) -> list[RetrievalHit]:
        cfg = self._config
        by_key = {_key(e.document.ref): e for e in eligible}
        lexical = bm25(query_tokens, {k: e.tokens for k, e in by_key.items()})
        similarity = {
            k: _finite(cosine(query_vector, e.vector)) for k, e in by_key.items()
        }
        lexical_pass = {
            k: s.score
            for k, s in lexical.items()
            if s.score > 0 and s.coverage >= cfg.min_lexical_coverage
        }
        semantic_pass = {k: s for k, s in similarity.items() if s >= cfg.min_similarity}
        lexical_rank = (
            rank(lexical_pass, cfg.channel_candidates)
            if LEXICAL in cfg.channels
            else []
        )
        semantic_rank = (
            rank(semantic_pass, cfg.channel_candidates)
            if SEMANTIC in cfg.channels
            else []
        )
        fused = reciprocal_rank_fusion(
            {LEXICAL: lexical_rank, SEMANTIC: semantic_rank}, cfg.rrf_k
        )
        hits: list[RetrievalHit] = []
        for key in rank(fused, len(fused)):
            entry = by_key[key]
            hits.append(
                RetrievalHit(
                    ref=entry.document.ref,
                    content_digest=entry.document.content_digest,
                    fused_score=fused[key],
                    lexical_rank=lexical_rank.index(key) + 1
                    if key in lexical_rank
                    else None,
                    lexical_coverage=lexical[key].coverage,
                    semantic_rank=semantic_rank.index(key) + 1
                    if key in semantic_rank
                    else None,
                    semantic_similarity=similarity[key],
                )
            )
        return hits

    async def _deliver(
        self,
        scope: ProductScope,
        context: ApplicabilityContext,
        hits: Sequence[RetrievalHit],
    ) -> tuple[list[GoldenExample], list[RetrievalHit], list[Refused]]:
        """Deliver in rank order, backfilling past entries that went stale."""
        want = self._config.max_results
        examples: list[GoldenExample] = []
        chosen: list[RetrievalHit] = []
        refused: list[Refused] = []
        position = 0
        while len(examples) < want and position < len(hits):
            batch = hits[position : position + want - len(examples)]
            position += len(batch)
            delivery = await self._reader.deliver(
                scope, context, [(h.ref, h.content_digest) for h in batch]
            )
            refused.extend(delivery.refused)
            by_ref = {h.ref: h for h in batch}
            for example in delivery.examples:
                examples.append(example)
                chosen.append(by_ref[example.ref])
        return examples, chosen, refused


def _key(ref: ExampleRef) -> str:
    return f"{ref.example_id}:{ref.version}"


def _finite(value: float) -> float:
    return value if math.isfinite(value) else 0.0
