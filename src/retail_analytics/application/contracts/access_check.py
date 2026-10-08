from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableMetadata:
    table: str
    rows: int
    columns: int
