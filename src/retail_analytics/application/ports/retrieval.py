from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


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
