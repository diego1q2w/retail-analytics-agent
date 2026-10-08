from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class BlobInfo:
    key: str
    size_bytes: int
    modified_at: datetime
