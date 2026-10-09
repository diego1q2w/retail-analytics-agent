from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TopicReset:
    session_id: str
    reset_id: str
    reset_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceListing:
    """One usable evidence record of the session, without its rows."""

    evidence_id: str
    version: int
    computed_at: datetime
    period: str | None
    definitions: tuple[str, ...]
    columns: tuple[str, ...]
    total_rows: int
    truncated_at_source: bool
    # Saved-report evidence from another session: "from saved report ...,
    # computed ...; historical snapshot". State it wherever its figures appear.
    source: str | None = None


@dataclass(frozen=True, slots=True)
class EvidencePage:
    """A bounded slice of one usable evidence record, screened like context."""

    evidence_id: str
    version: int
    computed_at: datetime
    period: str | None
    definitions: tuple[str, ...]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    offset: int
    total_rows: int
    # None when this page reaches the last stored row.
    next_offset: int | None
    # The stored result was already cut short at the source.
    truncated_at_source: bool
    notes: tuple[str, ...] = ()
    # Personal data or unknown references were masked in this page.
    masked: bool = False
    # Saved-report evidence from another session: "from saved report ...,
    # computed ...; historical snapshot". State it wherever its figures appear.
    source: str | None = None
