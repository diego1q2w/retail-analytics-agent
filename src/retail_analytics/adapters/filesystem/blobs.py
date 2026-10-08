"""``BlobStore`` on a local directory (or Docker volume).

Layout under the root: ``blobs/<artifact_id>/<sha256>`` for published content
and ``tmp/`` for writes in progress. Keys must match a strict pattern, so no
caller input can contain separators or ``..``; the resolved path is also
checked to stay inside the root, and files are opened without following
symlinks.

A write goes to a uniquely named file in ``tmp/``, is flushed and fsynced, and
is then hard-linked to its final name (which fails if the name exists) before
the temporary name is removed. Readers therefore see either nothing or the
complete file, a crash leaves only a ``tmp/`` file, and published content is
never overwritten.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from retail_analytics.application.artifacts import (
    BlobConflict,
    BlobError,
    BlobNotFound,
)
from retail_analytics.application.contracts.artifacts import BlobInfo

KEY_PATTERN = re.compile(r"([0-9a-f]{32})/([0-9a-f]{64})")
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_CHUNK = 1024 * 1024


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._blobs = root / "blobs"
        self._tmp = root / "tmp"
        for directory in (root, self._blobs, self._tmp):
            directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        self._root_real = root.resolve()

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put, key, data)

    async def get(self, key: str, *, max_bytes: int) -> bytes:
        return await asyncio.to_thread(self._get, key, max_bytes)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._exists, key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    async def list_blobs(self) -> list[BlobInfo]:
        return await asyncio.to_thread(self._list)

    async def remove_partials(self, older_than: datetime) -> int:
        return await asyncio.to_thread(self._remove_partials, older_than)

    def _path(self, key: str) -> Path:
        if KEY_PATTERN.fullmatch(key) is None:
            raise BlobError("invalid blob key")
        path = self._blobs / key
        if not path.parent.resolve().is_relative_to(self._root_real / "blobs"):
            raise BlobError("blob path escapes the storage root")
        return path

    def _put(self, key: str, data: bytes) -> None:
        final = self._path(key)
        try:
            final.parent.mkdir(mode=_DIR_MODE, exist_ok=True)
            final = self._path(key)  # re-check after the directory exists
            temporary = self._tmp / f"{uuid.uuid4().hex}.part"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _FILE_MODE,
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, final)
                except FileExistsError:
                    if not self._same_content(final, data):
                        raise BlobConflict(
                            f"blob {key!r} exists with other content"
                        ) from None
                else:
                    self._fsync_dir(final.parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
        except BlobError:
            raise
        except OSError as exc:
            raise BlobError(f"cannot write blob: {exc.strerror}") from None

    def _same_content(self, path: Path, data: bytes) -> bool:
        try:
            return self._read(path, len(data)) == data
        except BlobError:
            return False

    def _read(self, path: Path, max_bytes: int) -> bytes:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise BlobNotFound(path.name) from None
        except OSError as exc:
            raise BlobError(f"cannot open blob: {exc.strerror}") from None
        with os.fdopen(fd, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > max_bytes:
                raise BlobError("blob is larger than the allowed size")
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise BlobError("blob is larger than the allowed size")
        return data

    def _get(self, key: str, max_bytes: int) -> bytes:
        path = self._path(key)
        try:
            return self._read(path, max_bytes)
        except BlobNotFound:
            raise BlobNotFound(key) from None

    def _exists(self, key: str) -> bool:
        path = self._path(key)
        return path.is_file() and not path.is_symlink()

    def _delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BlobError(f"cannot delete blob: {exc.strerror}") from None
        with contextlib.suppress(OSError):
            path.parent.rmdir()  # only succeeds when empty

    def _list(self) -> list[BlobInfo]:
        found: list[BlobInfo] = []
        for directory in sorted(self._blobs.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in sorted(directory.iterdir()):
                key = f"{directory.name}/{path.name}"
                if KEY_PATTERN.fullmatch(key) is None or not path.is_file():
                    continue
                stat = path.stat()
                found.append(
                    BlobInfo(
                        key,
                        stat.st_size,
                        datetime.fromtimestamp(stat.st_mtime, UTC),
                    )
                )
        return found

    def _remove_partials(self, older_than: datetime) -> int:
        removed = 0
        for path in self._tmp.iterdir():
            if not path.name.endswith(".part") or path.is_symlink():
                continue
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < older_than:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
