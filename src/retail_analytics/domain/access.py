"""Server-side authority of an executive: roles, permissions and product scope.

Executives are the assistant's users, not the dataset's customers. What an
executive may do (permissions from server-assigned roles) and which products'
data they may see (product entitlements) are both owned by the application and
versioned together; tokens and model output never set them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class ProductScope:
    """Products an executive may see, resolved by trusted application code.

    An empty scope means no product data at all, never unrestricted access.
    """

    product_ids: frozenset[str]
    entitlement_version: int

    def __post_init__(self) -> None:
        if self.entitlement_version < 0:
            raise ValueError("entitlement_version must be non-negative")

    @property
    def is_empty(self) -> bool:
        return not self.product_ids


class Permission(StrEnum):
    """Operations an executive may perform (OAuth-style scope strings)."""

    ANALYSIS_READ = "analysis:read"
    REPORTS_READ_OWN = "reports:read_own"
    REPORTS_DELETE_OWN = "reports:delete_own"
    PERSONA_EDIT = "persona:edit"
    KNOWLEDGE_REVIEW = "knowledge:review"
    ACCESS_ADMIN = "access:admin"


class Role(StrEnum):
    EXECUTIVE = "executive"
    # Drafts, previews and publishes persona versions.
    EDITOR = "editor"
    # Reviews analytical definitions and Golden examples.
    REVIEWER = "reviewer"
    # Manages executives, roles and entitlements. Grants no product data.
    ADMIN = "admin"


ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.EXECUTIVE: frozenset(
        {
            Permission.ANALYSIS_READ,
            Permission.REPORTS_READ_OWN,
            Permission.REPORTS_DELETE_OWN,
        }
    ),
    Role.EDITOR: frozenset({Permission.PERSONA_EDIT}),
    Role.REVIEWER: frozenset({Permission.KNOWLEDGE_REVIEW}),
    Role.ADMIN: frozenset({Permission.ACCESS_ADMIN}),
}


def permissions_for(roles: frozenset[Role]) -> frozenset[Permission]:
    granted: frozenset[Permission] = frozenset()
    for role in roles:
        granted |= ROLE_PERMISSIONS[role]
    return granted


def is_valid_product_id(product_id: str) -> bool:
    """Product IDs are positive decimal integers kept as text."""
    return (
        0 < len(product_id) <= 19
        and product_id.isascii()
        and product_id.isdigit()
        and not product_id.startswith("0")
    )


@dataclass(frozen=True, slots=True)
class ExecutiveAccess:
    """Current server-side authority of one executive.

    ``authorization_version`` increases on every change to roles, products or
    status, so anything derived from an older version (cached evidence,
    filtered schema, model context) can be recognized as stale.
    """

    executive_id: str
    roles: frozenset[Role]
    product_ids: frozenset[str]
    active: bool
    authorization_version: int

    def __post_init__(self) -> None:
        if not self.executive_id:
            raise ValueError("executive_id is required")
        if self.authorization_version < 0:
            raise ValueError("authorization_version must be non-negative")

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.roles) if self.active else frozenset()

    @property
    def product_scope(self) -> ProductScope:
        products = self.product_ids if self.active else frozenset()
        return ProductScope(products, self.authorization_version)
