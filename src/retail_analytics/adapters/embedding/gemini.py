"""Gemini embeddings through google-genai (``gemini-embedding-2``).

The model takes task instructions in the text itself rather than a task-type
parameter. Free-tier keys are rate limited, so 429/503 responses are retried
with exponential backoff; the caller sees a failure if retries run out.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence

from google import genai
from google.genai import errors, types

DEFAULT_MODEL = "gemini-embedding-2"
_RETRYABLE = frozenset({429, 500, 503})


class GeminiEmbedder:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int = 768,
        max_attempts: int = 5,
        base_delay: float = 2.0,
        batch_size: int = 16,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._dimensions = dimensions
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._batch_size = batch_size

    @property
    def model_id(self) -> str:
        return f"{self._model}-{self._dimensions}"

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        formatted = [f"title: none | text: {t}" for t in texts]
        out: list[list[float]] = []
        for start in range(0, len(formatted), self._batch_size):
            out.extend(await self._embed(formatted[start : start + self._batch_size]))
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([f"task: search result | query: {text}"]))[0]

    async def _embed(self, contents: list[str]) -> list[list[float]]:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.aio.models.embed_content(
                    model=self._model,
                    contents=list(contents),
                    config=types.EmbedContentConfig(
                        output_dimensionality=self._dimensions
                    ),
                )
            except errors.APIError as error:
                if error.code not in _RETRYABLE or attempt == self._max_attempts:
                    raise
                await asyncio.sleep(self._base_delay * 2 ** (attempt - 1))
                continue
            vectors = [list(e.values or []) for e in response.embeddings or []]
            if len(vectors) != len(contents) or not all(vectors):
                raise ValueError("embedding response has the wrong shape")
            return [_unit(v) for v in vectors]
        raise AssertionError("unreachable")


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return vector if norm == 0 else [x / norm for x in vector]
