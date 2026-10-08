from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from retail_analytics.application.contracts.artifacts import BlobInfo
from retail_analytics.domain.artifacts import ArtifactVersion


class BlobStore(Protocol):
    """Immutable byte storage addressed by server-derived keys."""

    async def put(self, key: str, data: bytes) -> None:
        """Atomically publish ``data``; idempotent for identical content.

        Raises ``BlobConflict`` if the key holds different content.
        """
        ...

    async def get(self, key: str, *, max_bytes: int) -> bytes:
        """Read at most ``max_bytes``; larger or missing blobs raise."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> None:
        """Remove a blob; missing is not an error."""
        ...

    async def list_blobs(self) -> Sequence[BlobInfo]: ...

    async def remove_partials(self, older_than: datetime) -> int:
        """Delete abandoned temporary files; returns how many."""
        ...


class ArtifactCatalog(Protocol):
    """Metadata of versions (PostgreSQL). Never holds content."""

    async def owner_of(self, artifact_id: str) -> str | None: ...

    async def find_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> ArtifactVersion | None: ...

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
        """Append the next version (serialized per artifact).

        An existing (owner, key) returns the original row. A different owner
        for an existing artifact raises ``AccessDenied``.
        """
        ...

    async def get(
        self, artifact_id: str, version: int | None = None
    ) -> ArtifactVersion | None:
        """The given version, or the latest when ``version`` is ``None``."""
        ...

    async def versions(self, artifact_id: str) -> Sequence[ArtifactVersion]: ...

    async def referenced(self, keys: Sequence[str]) -> frozenset[str]: ...

    async def storage_keys(self, after: str | None, limit: int) -> Sequence[str]:
        """Distinct storage keys in order, after ``after`` (paging)."""
        ...

    async def delete_artifact(self, artifact_id: str) -> Sequence[str]:
        """Remove all versions' metadata; returns their storage keys."""
        ...
