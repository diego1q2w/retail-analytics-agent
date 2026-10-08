from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    issuer: str
    subject: str
    scopes: frozenset[str]
    expires_at: datetime
