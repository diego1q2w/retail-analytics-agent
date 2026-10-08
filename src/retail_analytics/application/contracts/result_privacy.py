from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import (
    date,
    datetime,
)
from decimal import Decimal

type Cell = str | int | float | bool | Decimal | date | datetime | None


@dataclass(frozen=True, slots=True)
class QueryRows:
    """What an executor read back, before any privacy check.

    ``complete`` is False when the executor stopped reading early (for example
    at its own page limit); the release is then marked truncated.
    """

    columns: tuple[str, ...]
    rows: Sequence[Sequence[object]]
    complete: bool = True

    def __repr__(self) -> str:
        return f"QueryRows(columns={self.columns}, rows=<{len(self.rows)} rows>)"
