"""Artifact versions: immutable stored content owned by one executive."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

MARKDOWN = "text/markdown"
PNG = "image/png"
JPEG = "image/jpeg"
PDF = "application/pdf"

ARTIFACT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def is_valid_artifact_id(value: str) -> bool:
    return ARTIFACT_ID_PATTERN.fullmatch(value) is not None


def storage_key_for(artifact_id: str, sha256: str) -> str:
    """Server-derived storage key; never built from user-supplied names."""
    if (
        not is_valid_artifact_id(artifact_id)
        or SHA256_PATTERN.fullmatch(sha256) is None
    ):
        raise ValueError("invalid artifact id or checksum")
    return f"{artifact_id}/{sha256}"


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    """One immutable version. The bytes live in the blob store, not here.

    ``storage_key`` is derived from the artifact ID and checksum, so a version
    never points outside its own artifact.
    """

    artifact_id: str
    version: int
    owner_id: str
    media_type: str
    sha256: str
    size_bytes: int
    storage_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1 or self.size_bytes < 0:
            raise ValueError("version must be >= 1 and size >= 0")
        if self.storage_key != storage_key_for(self.artifact_id, self.sha256):
            raise ValueError("storage key does not match artifact and checksum")
