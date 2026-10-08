"""Artifact service and local blob store: ownership, limits, atomicity, repair."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.application.artifacts import (
    ArtifactError,
    ArtifactErrorCode,
    ArtifactMaintenance,
    ArtifactPolicy,
    ArtifactService,
    BlobConflict,
    BlobError,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.domain.artifacts import ArtifactVersion

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 8, tzinfo=UTC)
A, B = "exec-a", "exec-b"


class MemoryCatalog:
    def __init__(self) -> None:
        self.rows: list[ArtifactVersion] = []
        self.keys: dict[tuple[str, str], ArtifactVersion] = {}
        self.fail_next = False

    async def owner_of(self, artifact_id: str) -> str | None:
        return next(
            (r.owner_id for r in self.rows if r.artifact_id == artifact_id), None
        )

    async def find_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> ArtifactVersion | None:
        return self.keys.get((owner_id, idempotency_key))

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
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("metadata commit failed")
        if (found := self.keys.get((owner_id, idempotency_key))) is not None:
            return found
        mine = [r for r in self.rows if r.artifact_id == artifact_id]
        if any(r.owner_id != owner_id for r in mine):
            raise AccessDenied("artifact", artifact_id)
        row = ArtifactVersion(
            artifact_id, len(mine) + 1, owner_id, media_type, sha256, size_bytes,
            f"{artifact_id}/{sha256}", NOW,
        )  # fmt: skip
        self.rows.append(row)
        self.keys[(owner_id, idempotency_key)] = row
        return row

    async def get(
        self, artifact_id: str, version: int | None = None
    ) -> ArtifactVersion | None:
        mine = [r for r in self.rows if r.artifact_id == artifact_id]
        if version is not None:
            return next((r for r in mine if r.version == version), None)
        return mine[-1] if mine else None

    async def versions(self, artifact_id: str) -> Sequence[ArtifactVersion]:
        return [r for r in self.rows if r.artifact_id == artifact_id]

    async def referenced(self, keys: Sequence[str]) -> frozenset[str]:
        return frozenset(r.storage_key for r in self.rows) & frozenset(keys)

    async def storage_keys(self, after: str | None, limit: int) -> Sequence[str]:
        keys = sorted({r.storage_key for r in self.rows})
        return [k for k in keys if after is None or k > after][:limit]

    async def delete_artifact(self, artifact_id: str) -> Sequence[str]:
        gone = [r for r in self.rows if r.artifact_id == artifact_id]
        self.rows = [r for r in self.rows if r.artifact_id != artifact_id]
        return sorted({r.storage_key for r in gone})


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


def make(
    root: Path, catalog: MemoryCatalog | None = None, **limits: int
) -> tuple[ArtifactService, MemoryCatalog, LocalBlobStore]:
    catalog = catalog or MemoryCatalog()
    blobs = LocalBlobStore(root)
    policy = ArtifactPolicy.with_limits(
        markdown=limits.get("markdown", 2048), binary=limits.get("binary", 4096)
    )
    return ArtifactService(catalog, blobs, policy), catalog, blobs


async def test_save_read_versions_and_restart(root: Path) -> None:
    service, catalog, _ = make(root)
    v1 = await service.save(
        A, media_type="text/markdown", content=b"# one", idempotency_key="k1"
    )
    v2 = await service.save(
        A, media_type="text/markdown", content=b"# two", idempotency_key="k2",
        artifact_id=v1.artifact_id,
    )  # fmt: skip
    assert (v1.version, v2.version) == (1, 2)
    assert v1.sha256 == hashlib.sha256(b"# one").hexdigest()
    restarted, _, _ = make(root, catalog)  # new store instance, same directory
    assert (await restarted.read(A, v1.artifact_id, 1)).content == b"# one"
    assert (await restarted.read(A, v1.artifact_id)).content == b"# two"
    assert [v.version for v in await restarted.versions(A, v1.artifact_id)] == [1, 2]


async def test_other_executive_cannot_read_or_overwrite(root: Path) -> None:
    service, _, _ = make(root)
    v = await service.save(
        A, media_type="text/markdown", content=b"secret", idempotency_key="k"
    )
    with pytest.raises(AccessDenied):
        await service.read(B, v.artifact_id)
    with pytest.raises(AccessDenied):
        await service.save(
            B, media_type="text/markdown", content=b"evil", idempotency_key="b1",
            artifact_id=v.artifact_id,
        )  # fmt: skip
    with pytest.raises(AccessDenied):
        await service.read(B, "f" * 32)  # missing looks the same
    assert (await service.read(A, v.artifact_id)).content == b"secret"


async def test_same_content_by_other_owner_is_separate_and_unreadable(
    root: Path,
) -> None:
    service, _, _ = make(root)
    a = await service.save(
        A, media_type="text/markdown", content=b"same", idempotency_key="k"
    )
    b = await service.save(
        B, media_type="text/markdown", content=b"same", idempotency_key="k"
    )
    assert a.artifact_id != b.artifact_id
    with pytest.raises(AccessDenied):
        await service.read(B, a.artifact_id)


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "", "A" * 32, "g" * 32, "0" * 31])
async def test_traversal_ids_are_rejected(root: Path, bad: str) -> None:
    service, _, blobs = make(root)
    with pytest.raises(AccessDenied):
        await service.read(A, bad)
    with pytest.raises(ArtifactError) as err:
        await service.save(
            A,
            media_type="text/markdown",
            content=b"x",
            idempotency_key="k",
            artifact_id=bad,
        )
    assert err.value.code is ArtifactErrorCode.INVALID_REQUEST
    for key in ("../../etc/passwd", "a" * 32 + "/../" + "b" * 64, "/abs"):
        with pytest.raises(BlobError):
            await blobs.put(key, b"x")
        with pytest.raises(BlobError):
            await blobs.get(key, max_bytes=10)


async def test_symlinked_blob_is_not_followed(root: Path, tmp_path: Path) -> None:
    _, _, blobs = make(root)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"outside")
    key = "a" * 32 + "/" + "b" * 64
    (root / "blobs" / ("a" * 32)).mkdir()
    os.symlink(secret, root / "blobs" / key)
    with pytest.raises(BlobError):
        await blobs.get(key, max_bytes=100)


async def test_size_and_type_failures_are_structured(root: Path) -> None:
    service, _, _ = make(root, markdown=10)

    async def fail(media_type: str, content: bytes) -> ArtifactError:
        with pytest.raises(ArtifactError) as err:
            await service.save(
                A, media_type=media_type, content=content, idempotency_key="k"
            )
        return err.value

    big = await fail("text/markdown", b"x" * 11)
    assert (big.code, big.limit, big.actual) == (ArtifactErrorCode.TOO_LARGE, 10, 11)
    assert (await fail("text/html", b"<b>")).code is (
        ArtifactErrorCode.UNSUPPORTED_MEDIA_TYPE
    )
    assert (await fail("text/markdown", b"")).code is ArtifactErrorCode.EMPTY
    assert (await fail("text/markdown", b"\xff\xfe")).code is (
        ArtifactErrorCode.INVALID_CONTENT
    )
    assert (await fail("application/pdf", b"not pdf")).code is (
        ArtifactErrorCode.INVALID_CONTENT
    )
    assert not list((root / "blobs").iterdir())  # nothing stored on failure


async def test_future_binary_types_are_accepted_by_signature(root: Path) -> None:
    service, _, _ = make(root)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    v = await service.save(A, media_type="image/png", content=png, idempotency_key="p")
    assert (await service.read(A, v.artifact_id)).content == png


async def test_retry_is_idempotent_and_conflicts_are_detected(root: Path) -> None:
    service, catalog, _ = make(root)
    first = await service.save(
        A, media_type="text/markdown", content=b"r", idempotency_key="op"
    )
    again = await service.save(
        A, media_type="text/markdown", content=b"r", idempotency_key="op"
    )
    assert again == first and len(catalog.rows) == 1
    with pytest.raises(ArtifactError) as err:
        await service.save(
            A, media_type="text/markdown", content=b"other", idempotency_key="op"
        )
    assert err.value.code is ArtifactErrorCode.IDEMPOTENCY_CONFLICT


async def test_failed_metadata_commit_publishes_nothing_and_retry_succeeds(
    root: Path,
) -> None:
    service, catalog, blobs = make(root)
    catalog.fail_next = True
    with pytest.raises(RuntimeError):
        await service.save(
            A, media_type="text/markdown", content=b"body", idempotency_key="op"
        )
    assert catalog.rows == []  # no reference exists
    assert len(await blobs.list_blobs()) == 1  # orphan blob, reconciled below
    v = await service.save(
        A, media_type="text/markdown", content=b"body", idempotency_key="op"
    )
    assert (await service.read(A, v.artifact_id)).content == b"body"
    assert len(await blobs.list_blobs()) == 2  # retry used a new artifact id


async def test_interrupted_write_leaves_no_published_blob(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, blobs = make(root)
    key = "a" * 32 + "/" + "b" * 64

    def crash(fd: int) -> None:
        raise OSError(5, "simulated crash during write")

    monkeypatch.setattr(os, "fsync", crash)
    with pytest.raises(BlobError):
        await blobs.put(key, b"partial")
    monkeypatch.undo()
    assert not await blobs.exists(key)
    assert await blobs.list_blobs() == []
    # a crash that skipped cleanup entirely leaves only an old .part file
    (root / "tmp" / "dead.part").write_bytes(b"half")
    removed = await blobs.remove_partials(datetime.now(UTC) + timedelta(seconds=5))
    assert removed == 1
    await blobs.put(key, b"complete")
    assert await blobs.get(key, max_bytes=100) == b"complete"


async def test_blob_is_immutable(root: Path) -> None:
    _, _, blobs = make(root)
    key = "a" * 32 + "/" + "b" * 64
    await blobs.put(key, b"one")
    await blobs.put(key, b"one")  # idempotent
    with pytest.raises(BlobConflict):
        await blobs.put(key, b"two")
    assert await blobs.get(key, max_bytes=10) == b"one"
    with pytest.raises(BlobError):
        await blobs.get(key, max_bytes=2)  # bounded read


async def test_tampered_content_fails_checksum(root: Path) -> None:
    service, _, _ = make(root)
    v = await service.save(
        A, media_type="text/markdown", content=b"abc", idempotency_key="k"
    )
    (root / "blobs" / v.storage_key).write_bytes(b"xyz")
    with pytest.raises(ArtifactError) as err:
        await service.read(A, v.artifact_id)
    assert err.value.code is ArtifactErrorCode.INTEGRITY_FAILURE


async def test_reconcile_orphans_partials_and_missing(root: Path) -> None:
    service, catalog, blobs = make(root)
    kept = await service.save(
        A, media_type="text/markdown", content=b"keep", idempotency_key="k"
    )
    lost = await service.save(
        A, media_type="text/markdown", content=b"lost", idempotency_key="l"
    )
    catalog.fail_next = True
    with pytest.raises(RuntimeError):
        await service.save(
            A, media_type="text/markdown", content=b"orphan", idempotency_key="o"
        )
    (root / "tmp" / "x.part").write_bytes(b"p")
    (root / "blobs" / lost.storage_key).unlink()

    now = datetime.now(UTC)
    fresh = ArtifactMaintenance(catalog, blobs, clock=lambda: now)
    report = await fresh.reconcile(grace=timedelta(hours=1))
    assert (report.partials_removed, report.orphans_removed) == (0, 0)  # in-flight safe

    later = ArtifactMaintenance(catalog, blobs, clock=lambda: now + timedelta(hours=2))
    report = await later.reconcile(grace=timedelta(hours=1))
    assert (report.partials_removed, report.orphans_removed) == (1, 1)
    assert report.missing_content == (lost.storage_key,)
    assert await blobs.exists(kept.storage_key)
    with pytest.raises(ArtifactError) as err:
        await service.read(A, lost.artifact_id)
    assert err.value.code is ArtifactErrorCode.CONTENT_MISSING


async def test_purge_removes_metadata_and_bytes(root: Path) -> None:
    service, catalog, blobs = make(root)
    v = await service.save(
        A, media_type="text/markdown", content=b"gone", idempotency_key="k"
    )
    assert await ArtifactMaintenance(catalog, blobs).purge(v.artifact_id) == 1
    assert not await blobs.exists(v.storage_key)
    with pytest.raises(AccessDenied):
        await service.read(A, v.artifact_id)
