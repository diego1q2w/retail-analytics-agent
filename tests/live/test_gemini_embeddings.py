"""Live Gemini embedding check: two requests in total (free-tier rate limits).

Skipped without a key. Reads the ignored ``.env`` through the typed loader.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.adapters.embedding.gemini import GeminiEmbedder
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.domain.retrieval import cosine

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_gemini_embeddings_rank_a_paraphrase_above_an_unrelated_text() -> None:
    settings = load_backend_settings(environ={}, env_file=ROOT / ".env")
    if settings.gemini_api_key is None:
        pytest.skip("RETAIL_ANALYTICS_GEMINI_API_KEY not set")
    embedder = GeminiEmbedder(
        settings.gemini_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    docs = await embedder.embed_documents(
        [
            "How did monthly revenue trend over the last year?",
            "Which product categories have the highest return rates?",
        ]
    )
    query = await embedder.embed_query("Is our sales growing from month to month?")
    assert len(query) == settings.embedding_dimensions
    assert cosine(query, docs[0]) > cosine(query, docs[1])
