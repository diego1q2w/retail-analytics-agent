"""Query-embedding cache so evaluation sweeps embed each question once.

Free-tier embedding quotas are small and a threshold sweep asks for the same
question vector many times. Keys are ``sha256(model id | text)``; the file holds
vectors only, no question text. Document embeddings pass straight through
because the Postgres store already caches those by content digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from retail_analytics.application.retrieval import TextEmbedder


class CachedQueryEmbedder:
    def __init__(self, inner: TextEmbedder, path: Path) -> None:
        self._inner = inner
        self._path = path
        self._cache: dict[str, list[float]] = {}
        self.calls = 0
        if path.exists():
            self._cache = json.loads(path.read_text(encoding="utf-8"))

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        key = hashlib.sha256(f"{self.model_id}|{text}".encode()).hexdigest()
        if key not in self._cache:
            self._cache[key] = await self._inner.embed_query(text)
            self.calls += 1
            self._save()
        return self._cache[key]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._cache), encoding="utf-8")
        tmp.replace(self._path)
