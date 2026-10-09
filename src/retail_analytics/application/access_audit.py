"""Administrator view of who changed an executive's access."""

from __future__ import annotations

from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.contracts.access_audit import (
    MAX_HISTORY,
    AccessChange,
)
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.ports.access_audit import AccessChangeHistory
from retail_analytics.domain.access import Permission


class AccessAuditService:
    def __init__(self, history: AccessChangeHistory, resolver: AccessResolver) -> None:
        self._history = history
        self._resolver = resolver

    async def history(
        self, principal: Principal, executive_id: str, *, limit: int = 50
    ) -> list[AccessChange]:
        """Access changes for ``executive_id``, newest first.

        Needs ``access:admin`` (current role and token scope); anyone else
        gets ``AccessDenied``.
        """
        await self._resolver.require_permission(principal, Permission.ACCESS_ADMIN)
        if not 1 <= limit <= MAX_HISTORY:
            raise ValueError(f"limit must be between 1 and {MAX_HISTORY}")
        return await self._history.history(executive_id, limit=limit)
