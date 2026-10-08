"""Versioned artifact storage behind narrow ports.

``ArtifactService`` is what other capabilities (reports, cleanup, Golden
Knowledge) use. It validates type and size, writes the bytes to a ``BlobStore``
*before* the metadata row that publishes them, and checks ownership on every
read. A write interrupted at any point therefore leaves at most an unreferenced
blob or temporary file, never a reference to partial content. Retries with the
same idempotency key return the original version.

Reconciliation (``ArtifactMaintenance.reconcile``):
- temporary partial files older than the grace period are removed;
- blobs no metadata row references (a failed metadata commit) and older than
  the grace period are removed;
- metadata whose blob is missing is reported, never silently repaired.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from retail_analytics.application.authorization import AccessDenied, require_owner
from retail_analytics.application.ports.artifacts import (
    ArtifactCatalog,
    BlobStore,
)
from retail_analytics.domain.artifacts import (
    JPEG,
    MARKDOWN,
    PDF,
    PNG,
    ArtifactVersion,
    is_valid_artifact_id,
    storage_key_for,
)

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:\-]{1,200}")
DEFAULT_MARKDOWN_LIMIT = 1024 * 1024
DEFAULT_BINARY_LIMIT = 10 * 1024 * 1024


class ArtifactErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    EMPTY = "empty"
    TOO_LARGE = "too_large"
    INVALID_CONTENT = "invalid_content"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CONTENT_MISSING = "content_missing"
    INTEGRITY_FAILURE = "integrity_failure"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class ArtifactError(Exception):
    """Structured failure. ``limit``/``actual`` are set for size failures."""

    def __init__(
        self,
        code: ArtifactErrorCode,
        message: str,
        *,
        limit: int | None = None,
        actual: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.limit = limit
        self.actual = actual
        super().__init__(f"{code.value}: {message}")


class BlobError(Exception):
    """The blob store failed (I/O, permissions, bad key)."""


class BlobNotFound(BlobError):
    def __init__(self, key: str) -> None:
        super().__init__(f"blob {key!r} not found")


class BlobConflict(BlobError):
    """A blob with this key exists with different content."""


@dataclass(frozen=True, slots=True)
class ArtifactPolicy:
    """Allowed media types and their size limits in bytes."""

    max_bytes: Mapping[str, int] = field(
        default_factory=lambda: {
            MARKDOWN: DEFAULT_MARKDOWN_LIMIT,
            PNG: DEFAULT_BINARY_LIMIT,
            JPEG: DEFAULT_BINARY_LIMIT,
            PDF: DEFAULT_BINARY_LIMIT,
        }
    )

    @classmethod
    def with_limits(cls, *, markdown: int, binary: int) -> ArtifactPolicy:
        return cls(
            {MARKDOWN: markdown, PNG: binary, JPEG: binary, PDF: binary},
        )

    def validate(self, media_type: str, content: bytes) -> None:
        limit = self.max_bytes.get(media_type)
        if limit is None:
            raise ArtifactError(
                ArtifactErrorCode.UNSUPPORTED_MEDIA_TYPE,
                f"media type {media_type!r} is not supported",
            )
        if not content:
            raise ArtifactError(ArtifactErrorCode.EMPTY, "content is empty")
        if len(content) > limit:
            raise ArtifactError(
                ArtifactErrorCode.TOO_LARGE,
                f"content exceeds the {limit}-byte limit for {media_type}",
                limit=limit,
                actual=len(content),
            )
        if not _plausible(media_type, content):
            raise ArtifactError(
                ArtifactErrorCode.INVALID_CONTENT,
                f"content is not valid {media_type}",
            )


_MAGIC: dict[str, bytes] = {
    PNG: b"\x89PNG\r\n\x1a\n",
    JPEG: b"\xff\xd8\xff",
    PDF: b"%PDF-",
}


def _plausible(media_type: str, content: bytes) -> bool:
    if media_type == MARKDOWN:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return "\x00" not in text
    return content.startswith(_MAGIC[media_type])


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    version: ArtifactVersion
    content: bytes


class ArtifactService:
    """Save, read and list artifacts for their owner."""

    def __init__(
        self,
        catalog: ArtifactCatalog,
        blobs: BlobStore,
        policy: ArtifactPolicy,
        *,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._catalog = catalog
        self._blobs = blobs
        self._policy = policy
        self._new_id = new_id

    async def save(
        self,
        executive_id: str,
        *,
        media_type: str,
        content: bytes,
        idempotency_key: str,
        artifact_id: str | None = None,
    ) -> ArtifactVersion:
        """Store ``content`` as a new version (version 1 when new).

        ``idempotency_key`` (for example the operation ID) is scoped to the
        executive: repeating it with identical content returns the original
        version; with different content it fails with ``idempotency_conflict``.
        """
        if not executive_id:
            raise AccessDenied("artifact", artifact_id or "")
        if IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
            raise ArtifactError(
                ArtifactErrorCode.INVALID_REQUEST, "invalid idempotency key"
            )
        if artifact_id is not None and not is_valid_artifact_id(artifact_id):
            raise ArtifactError(
                ArtifactErrorCode.INVALID_REQUEST, "invalid artifact id"
            )
        self._policy.validate(media_type, content)
        sha256 = hashlib.sha256(content).hexdigest()

        existing = await self._catalog.find_by_idempotency_key(
            executive_id, idempotency_key
        )
        if existing is not None:
            self._require_same(existing, artifact_id, media_type, sha256)
            await self._publish_blob(existing.storage_key, content)  # heal if lost
            return existing

        target = artifact_id or self._new_id()
        owner = await self._catalog.owner_of(target)
        if owner is not None:
            require_owner("artifact", target, owner, executive_id)
        key = storage_key_for(target, sha256)
        await self._publish_blob(key, content)
        version = await self._catalog.add_version(
            owner_id=executive_id,
            artifact_id=target,
            media_type=media_type,
            sha256=sha256,
            size_bytes=len(content),
            idempotency_key=idempotency_key,
        )
        self._require_same(version, artifact_id, media_type, sha256)
        return version

    async def read(
        self, executive_id: str, artifact_id: str, version: int | None = None
    ) -> ArtifactContent:
        meta = await self.describe(executive_id, artifact_id, version)
        try:
            data = await self._blobs.get(meta.storage_key, max_bytes=meta.size_bytes)
        except BlobNotFound:
            raise ArtifactError(
                ArtifactErrorCode.CONTENT_MISSING, "stored content is missing"
            ) from None
        except BlobError:
            raise ArtifactError(
                ArtifactErrorCode.STORAGE_UNAVAILABLE, "storage is unavailable"
            ) from None
        if (
            len(data) != meta.size_bytes
            or hashlib.sha256(data).hexdigest() != meta.sha256
        ):
            raise ArtifactError(
                ArtifactErrorCode.INTEGRITY_FAILURE, "stored content failed checksum"
            )
        return ArtifactContent(meta, data)

    async def describe(
        self, executive_id: str, artifact_id: str, version: int | None = None
    ) -> ArtifactVersion:
        meta = (
            await self._catalog.get(artifact_id, version)
            if is_valid_artifact_id(artifact_id)
            else None
        )
        if meta is None:
            raise AccessDenied("artifact", artifact_id)
        require_owner("artifact", artifact_id, meta.owner_id, executive_id)
        return meta

    async def versions(
        self, executive_id: str, artifact_id: str
    ) -> Sequence[ArtifactVersion]:
        await self.describe(executive_id, artifact_id)
        return await self._catalog.versions(artifact_id)

    async def _publish_blob(self, key: str, content: bytes) -> None:
        try:
            await self._blobs.put(key, content)
        except BlobConflict:
            raise ArtifactError(
                ArtifactErrorCode.INTEGRITY_FAILURE,
                "stored content differs from its checksum",
            ) from None
        except BlobError:
            raise ArtifactError(
                ArtifactErrorCode.STORAGE_UNAVAILABLE, "storage is unavailable"
            ) from None

    @staticmethod
    def _require_same(
        stored: ArtifactVersion,
        artifact_id: str | None,
        media_type: str,
        sha256: str,
    ) -> None:
        if (
            stored.sha256 != sha256
            or stored.media_type != media_type
            or (artifact_id is not None and stored.artifact_id != artifact_id)
        ):
            raise ArtifactError(
                ArtifactErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key was used for different content",
            )


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    partials_removed: int
    orphans_removed: int
    missing_content: tuple[str, ...]


class ArtifactMaintenance:
    """Trusted maintenance only (cleanup, operators); never a model tool."""

    PAGE = 500

    def __init__(
        self,
        catalog: ArtifactCatalog,
        blobs: BlobStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._catalog = catalog
        self._blobs = blobs
        self._clock = clock

    async def reconcile(self, grace: timedelta = timedelta(hours=1)) -> ReconcileReport:
        """Remove old partials and unreferenced blobs; report missing content.

        The grace period protects writes in flight (blob written, metadata not
        yet committed).
        """
        cutoff = self._clock() - grace
        partials = await self._blobs.remove_partials(cutoff)
        old = [b.key for b in await self._blobs.list_blobs() if b.modified_at < cutoff]
        orphans = 0
        for start in range(0, len(old), self.PAGE):
            chunk = old[start : start + self.PAGE]
            referenced = await self._catalog.referenced(chunk)
            for key in chunk:
                if key not in referenced:
                    await self._blobs.delete(key)
                    orphans += 1
        missing: list[str] = []
        after: str | None = None
        while keys := await self._catalog.storage_keys(after, self.PAGE):
            missing.extend([k for k in keys if not await self._blobs.exists(k)])
            after = keys[-1]
        return ReconcileReport(partials, orphans, tuple(missing))

    async def purge(self, artifact_id: str) -> int:
        """Delete all versions of an artifact: metadata first, then bytes.

        A crash between the two leaves orphan blobs that ``reconcile`` removes.
        """
        keys = await self._catalog.delete_artifact(artifact_id)
        for key in keys:
            await self._blobs.delete(key)
        return len(keys)
