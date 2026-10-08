from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.domain.access import Role


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated executive and the operations their token allows.

    ``scopes`` is a ceiling, not a grant: effective permissions are the
    executive's current server-side permissions intersected with it. A
    principal may be stored with a run so retried activities can re-resolve
    authority without the original token.
    """

    executive_id: str
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not self.executive_id:
            raise ValueError("executive_id is required")


@dataclass(frozen=True, slots=True)
class ExecutiveRegistration:
    executive_id: str
    issuer: str
    subject: str
    roles: frozenset[Role]
    # Non-identifying label for operators, e.g. "Demo executive A".
    label: str

    def __post_init__(self) -> None:
        if not (self.executive_id and self.issuer and self.subject):
            raise ValueError("executive_id, issuer and subject are required")
        if not 0 < len(self.label) <= 120:
            raise ValueError("label must be 1-120 characters")
