"""PostgreSQL store for Golden Knowledge embeddings (vectors only)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import golden_embeddings as ge


class PostgresEmbeddingStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(
        self, digests: Sequence[str], model_id: str, dimensions: int
    ) -> dict[str, list[float]]:
        if not digests:
            return {}
        return await self._db.transaction(
            self._load, list(digests), model_id, dimensions
        )

    async def save(
        self, vectors: Mapping[str, Sequence[float]], model_id: str, dimensions: int
    ) -> None:
        if vectors:
            await self._db.transaction(self._save, dict(vectors), model_id, dimensions)

    @staticmethod
    def _load(
        connection: sa.Connection, digests: list[str], model_id: str, dimensions: int
    ) -> dict[str, list[float]]:
        rows = connection.execute(
            sa.select(ge.c.content_digest, ge.c.vector).where(
                ge.c.content_digest.in_(digests),
                ge.c.model_id == model_id,
                ge.c.dimensions == dimensions,
            )
        ).all()
        return {r[0]: [float(x) for x in r[1]] for r in rows}

    def _save(
        self,
        connection: sa.Connection,
        vectors: dict[str, Sequence[float]],
        model_id: str,
        dimensions: int,
    ) -> None:
        now = self._db.clock()
        connection.execute(
            insert(ge)
            .values(
                [
                    {
                        "content_digest": digest,
                        "model_id": model_id,
                        "dimensions": dimensions,
                        "vector": [float(x) for x in vector],
                        "created_at": now,
                    }
                    for digest, vector in vectors.items()
                ]
            )
            .on_conflict_do_nothing()
        )
