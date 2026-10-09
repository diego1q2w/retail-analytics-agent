"""The approved schema context shown to the model with every attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SchemaContextStatus(StrEnum):
    # Relations, fields, joins and metrics this executive may query now.
    AVAILABLE = "available"
    # The executive may not query any relation (no analysis permission or an
    # empty product scope): nothing is described.
    NO_ACCESS = "no_access"
    # No sufficiently fresh validated metadata: nothing is described (fails
    # closed exactly like the discovery tools and the query compiler).
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SchemaContext:
    """Sanitized approved schema description for one executive, right now.

    Contains logical names, types, descriptions, reviewed joins and approved
    metric definitions only: never source tables or columns, product IDs,
    entitlements or result rows.
    """

    status: SchemaContextStatus
    lines: tuple[str, ...]
    # Identity of the described content (catalog, availability, metrics); a
    # change means earlier schema observations may no longer hold.
    fingerprint: str
    metadata_as_of: datetime | None = None
    stale: bool = False
    # Relations left out to stay within the size bound.
    omitted_relations: int = 0

    @property
    def chars(self) -> int:
        return sum(len(line) + 1 for line in self.lines)
