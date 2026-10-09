"""Audit vocabulary for executive access changes (who can see what)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Actor recorded when an operator path (CLI, bootstrap) changes access without
# an authenticated executive.
SYSTEM_ACTOR = "system:operator"

SUBJECT_TYPE = "executive"
REGISTERED = "access.executive_registered"
ROLES_CHANGED = "access.roles_changed"
PROFILE_CHANGED = "access.profile_changed"
ENTITLEMENTS_CHANGED = "access.entitlements_changed"
ACTIVATED = "access.activated"
DEACTIVATED = "access.deactivated"

ACCESS_ACTIONS = (
    REGISTERED,
    ROLES_CHANGED,
    PROFILE_CHANGED,
    ENTITLEMENTS_CHANGED,
    ACTIVATED,
    DEACTIVATED,
)

MAX_HISTORY = 200


@dataclass(frozen=True, slots=True)
class AccessChange:
    """One recorded access mutation.

    ``details`` holds the change kind, role names, product counts and
    digests, and the old/new ``authorization_version``; never raw product
    lists, labels, identity-provider subjects or tokens.
    """

    audit_id: str
    occurred_at: datetime
    actor_id: str
    executive_id: str
    action: str
    details: dict[str, object]
