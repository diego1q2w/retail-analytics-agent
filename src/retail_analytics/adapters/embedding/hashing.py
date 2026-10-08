"""Deterministic, offline embedder for fixture mode and tests.

Feature-hashes content words and character trigrams into a fixed-size
unit vector. It captures lexical overlap and morphology only, not meaning;
it exists so the semantic channel is exercised reproducibly without a provider.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from retail_analytics.domain.retrieval import tokenize


class HashingEmbedder:
    def __init__(self, dimensions: int = 256) -> None:
        if dimensions < 16:
            raise ValueError("dimensions must be at least 16")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_id(self) -> str:
        return f"hashing-v1-{self._dimensions}"

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for token in tokenize(text):
            self._add(vector, "w:" + token, 1.0)
            padded = f"^{token}$"
            for i in range(len(padded) - 2):
                self._add(vector, "t:" + padded[i : i + 3], 0.35)
        norm = math.sqrt(sum(x * x for x in vector))
        return vector if norm == 0 else [x / norm for x in vector]

    def _add(self, vector: list[float], feature: str, weight: float) -> None:
        digest = hashlib.sha256(feature.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % self._dimensions
        vector[index] += weight if digest[4] & 1 else -weight
