"""Composition of authorized hybrid Golden retrieval."""

from __future__ import annotations

from retail_analytics.adapters.embedding.gemini import GeminiEmbedder
from retail_analytics.adapters.embedding.hashing import HashingEmbedder
from retail_analytics.application.retrieval import (
    GoldenIndex,
    GoldenRetriever,
    TextEmbedder,
)
from retail_analytics.bootstrap.config import BackendSettings, ConfigError
from retail_analytics.bootstrap.knowledge import KnowledgeServices
from retail_analytics.domain.retrieval import RetrievalConfig


def build_embedder(settings: BackendSettings) -> TextEmbedder:
    if settings.embedding_provider == "hashing":
        return HashingEmbedder()
    if settings.gemini_api_key is None:
        raise ConfigError(["RETAIL_ANALYTICS_GEMINI_API_KEY: required for gemini"])
    return GeminiEmbedder(
        settings.gemini_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )


def retrieval_config(settings: BackendSettings) -> RetrievalConfig:
    try:
        return RetrievalConfig(
            max_results=settings.retrieval_max_results,
            channel_candidates=settings.retrieval_channel_candidates,
            min_similarity=settings.retrieval_min_similarity,
            min_lexical_coverage=settings.retrieval_min_lexical_coverage,
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
    index = GoldenIndex(knowledge.index_source, embedder or build_embedder(settings))
    return GoldenRetriever(index, knowledge.reader, retrieval_config(settings))
