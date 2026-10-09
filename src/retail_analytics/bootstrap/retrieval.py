"""Composition of authorized hybrid Golden retrieval."""

from __future__ import annotations

from retail_analytics.adapters.embedding.gemini import GeminiEmbedder
from retail_analytics.adapters.embedding.hashing import HashingEmbedder
from retail_analytics.application.ports.retrieval import TextEmbedder
from retail_analytics.application.retrieval import (
    GoldenIndex,
    GoldenRetriever,
)
from retail_analytics.bootstrap.config import BackendSettings, ConfigError
from retail_analytics.bootstrap.knowledge import KnowledgeServices
from retail_analytics.domain.retrieval import (
    LEXICAL_MIN_COVERAGE,
    MEASURED_EMBEDDING_DIMENSIONS,
    MEASURED_EMBEDDING_MODEL,
    PLACEHOLDER_MIN_LEXICAL_COVERAGE,
    PLACEHOLDER_MIN_SIMILARITY,
    SEMANTIC_MIN_SIMILARITY,
    RetrievalConfig,
)


def build_embedder(settings: BackendSettings) -> TextEmbedder:
    if settings.embedding_provider == "hashing":
        return HashingEmbedder()
    if settings.gemini_api_key is None:
        raise ConfigError(["GEMINI_API_KEY: required for gemini"])
    return GeminiEmbedder(
        settings.gemini_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )


def retrieval_config(settings: BackendSettings) -> RetrievalConfig:
    # Cosine scales differ by model: the measured defaults are for Gemini
    # embeddings; the hashing embedder keeps the unmeasured T24 values.
    measured = settings.embedding_provider == "gemini"
    if (
        measured
        and (
            settings.embedding_model != MEASURED_EMBEDDING_MODEL
            or settings.embedding_dimensions != MEASURED_EMBEDDING_DIMENSIONS
        )
        and (
            settings.retrieval_min_similarity is None
            or settings.retrieval_min_lexical_coverage is None
        )
    ):
        raise ConfigError(
            [
                "RETRIEVAL_MIN_SIMILARITY and RETRIEVAL_MIN_LEXICAL_COVERAGE: "
                "the defaults were measured for "
                f"{MEASURED_EMBEDDING_MODEL} at {MEASURED_EMBEDDING_DIMENSIONS} "
                "dimensions; set both for another model or dimension count"
            ]
        )
    similarity = settings.retrieval_min_similarity
    if similarity is None:
        similarity = SEMANTIC_MIN_SIMILARITY if measured else PLACEHOLDER_MIN_SIMILARITY
    coverage = settings.retrieval_min_lexical_coverage
    if coverage is None:
        coverage = (
            LEXICAL_MIN_COVERAGE if measured else PLACEHOLDER_MIN_LEXICAL_COVERAGE
        )
    try:
        return RetrievalConfig(
            max_results=settings.retrieval_max_results,
            channel_candidates=settings.retrieval_channel_candidates,
            min_similarity=similarity,
            min_lexical_coverage=coverage,
            semantic_weight=settings.retrieval_semantic_weight,
        )
    except ValueError as error:
        raise ConfigError([f"retrieval settings: {error}"]) from None


def build_retrieval(
    settings: BackendSettings,
    knowledge: KnowledgeServices,
    *,
    embedder: TextEmbedder | None = None,
) -> GoldenRetriever:
    """Pass ``embedder`` to override the configured provider (tests)."""
    index = GoldenIndex(
        knowledge.index_source,
        embedder or build_embedder(settings),
        knowledge.embeddings,
    )
    return GoldenRetriever(index, knowledge.reader, retrieval_config(settings))
