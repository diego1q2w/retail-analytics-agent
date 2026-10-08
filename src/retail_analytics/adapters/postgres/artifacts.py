"""PostgreSQL ``ArtifactCatalog``: version metadata, never content."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import artifact_versions as av
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.domain.artifacts import ArtifactVersion


def _version(m: Mapping[Any, Any]) -> ArtifactVersion:
    return ArtifactVersion(
        artifact_id=m["artifact_id"],
        version=m["version"],
        owner_id=m["owner_id"],
        media_type=m["media_type"],
        sha256=m["sha256"],
        size_bytes=m["size_bytes"],
        storage_key=m["storage_key"],
        created_at=m["created_at"],
    )


def _select_columns() -> list[sa.Column[object]]:
    return [
        av.c.artifact_id,
        av.c.version,
        av.c.owner_id,
        av.c.media_type,
        av.c.sha256,
        av.c.size_bytes,
        av.c.storage_key,
        av.c.created_at,
    ]


class PostgresArtifactCatalog:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def owner_of(self, artifact_id: str) -> str | None:
        return await self._db.transaction(self._owner_of, artifact_id)

    async def find_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> ArtifactVersion | None:
        return await self._db.transaction(self._find, owner_id, idempotency_key)

    async def add_version(
        self,
        *,
        owner_id: str,
        artifact_id: str,
        media_type: str,
        sha256: str,
        size_bytes: int,
        idempotency_key: str,
    ) -> ArtifactVersion:
        return await self._db.transaction(
            self._add,
            owner_id,
            artifact_id,
            media_type,
            sha256,
            size_bytes,
            idempotency_key,
        )

    async def get(
        self, artifact_id: str, version: int | None = None
    ) -> ArtifactVersion | None:
        return await self._db.transaction(self._get, artifact_id, version)

    async def versions(self, artifact_id: str) -> Sequence[ArtifactVersion]:
        return await self._db.transaction(self._versions, artifact_id)

    async def referenced(self, keys: Sequence[str]) -> frozenset[str]:
        return await self._db.transaction(self._referenced, list(keys))

    async def storage_keys(self, after: str | None, limit: int) -> Sequence[str]:
        return await self._db.transaction(self._storage_keys, after, limit)

    async def delete_artifact(self, artifact_id: str) -> Sequence[str]:
        return await self._db.transaction(self._delete, artifact_id)

    @staticmethod
    def _owner_of(connection: sa.Connection, artifact_id: str) -> str | None:
        owner = connection.execute(
            sa.select(av.c.owner_id).where(av.c.artifact_id == artifact_id).limit(1)
        ).scalar_one_or_none()
        return None if owner is None else str(owner)

    @staticmethod
    def _find(
        connection: sa.Connection, owner_id: str, key: str
    ) -> ArtifactVersion | None:
        row = connection.execute(
            sa.select(*_select_columns()).where(
                av.c.owner_id == owner_id, av.c.idempotency_key == key
            )
        ).one_or_none()
        return None if row is None else _version(row._mapping)

    def _add(
        self,
        connection: sa.Connection,
        owner_id: str,
        artifact_id: str,
        media_type: str,
        sha256: str,
        size_bytes: int,
        key: str,
    ) -> ArtifactVersion:
        # Serialize writers of one artifact (also covers its first version).
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(artifact_id, 0))
            )
        )
        existing = self._find(connection, owner_id, key)
        if existing is not None:
            return existing
        current = connection.execute(
            sa.select(av.c.owner_id, sa.func.max(av.c.version))
            .where(av.c.artifact_id == artifact_id)
            .group_by(av.c.owner_id)
        ).all()
        if any(row[0] != owner_id for row in current):
            raise AccessDenied("artifact", artifact_id)
        next_version = (max((row[1] for row in current), default=0)) + 1
        inserted = connection.execute(
            insert(av)
            .values(
                artifact_id=artifact_id,
                version=next_version,
                owner_id=owner_id,
                media_type=media_type,
                sha256=sha256,
                size_bytes=size_bytes,
                storage_key=f"{artifact_id}/{sha256}",
                idempotency_key=key,
                created_at=self._db.clock(),
            )
            .on_conflict_do_nothing(constraint="uq_artifact_versions_idempotency")
            .returning(*_select_columns())
        ).one_or_none()
        if inserted is None:  # same key raced in through another artifact ID
            raced = self._find(connection, owner_id, key)
            assert raced is not None  # noqa: S101
            return raced
        return _version(inserted._mapping)

    @staticmethod
    def _get(
        connection: sa.Connection, artifact_id: str, version: int | None
    ) -> ArtifactVersion | None:
        query = sa.select(*_select_columns()).where(av.c.artifact_id == artifact_id)
        query = (
            query.where(av.c.version == version)
            if version is not None
            else query.order_by(av.c.version.desc()).limit(1)
        )
        row = connection.execute(query).one_or_none()
        return None if row is None else _version(row._mapping)

    @staticmethod
    def _versions(connection: sa.Connection, artifact_id: str) -> list[ArtifactVersion]:
        rows = connection.execute(
            sa.select(*_select_columns())
            .where(av.c.artifact_id == artifact_id)
            .order_by(av.c.version)
        ).all()
        return [_version(row._mapping) for row in rows]

    @staticmethod
    def _referenced(connection: sa.Connection, keys: list[str]) -> frozenset[str]:
        rows = connection.execute(
            sa.select(av.c.storage_key).where(av.c.storage_key.in_(keys)).distinct()
        ).scalars()
        return frozenset(str(key) for key in rows)

    @staticmethod
    def _storage_keys(
        connection: sa.Connection, after: str | None, limit: int
    ) -> list[str]:
        query = sa.select(av.c.storage_key).distinct().order_by(av.c.storage_key)
        if after is not None:
            query = query.where(av.c.storage_key > after)
        return [str(k) for k in connection.execute(query.limit(limit)).scalars()]

    @staticmethod
    def _delete(connection: sa.Connection, artifact_id: str) -> list[str]:
        rows = connection.execute(
            sa.delete(av)
            .where(av.c.artifact_id == artifact_id)
            .returning(av.c.storage_key)
        ).scalars()
        return sorted({str(k) for k in rows})
