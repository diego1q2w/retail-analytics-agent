from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.access_audit import AccessChange


class AccessChangeHistory(Protocol):
    async def history(self, executive_id: str, *, limit: int) -> list[AccessChange]:
        """Newest first."""
        ...
