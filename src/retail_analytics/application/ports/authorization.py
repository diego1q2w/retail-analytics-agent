from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from retail_analytics.application.contracts.access_audit import SYSTEM_ACTOR
from retail_analytics.application.contracts.authorization import ExecutiveRegistration
from retail_analytics.domain.access import ExecutiveAccess
from retail_analytics.domain.conversation import Session
from retail_analytics.domain.executions import ToolExecution
from retail_analytics.domain.runs import Run


class ExecutiveDirectory(Protocol):
    """Read side: current authority of executives."""

    async def find_by_subject(
        self, issuer: str, subject: str
    ) -> ExecutiveAccess | None: ...

    async def get(self, executive_id: str) -> ExecutiveAccess | None: ...


class AccessAdministration(Protocol):
    """Write side, for trusted operator paths only (never a model tool).

    Every effective change increments the executive's authorization version
    and writes one ``access.*`` audit event in the same transaction (the
    change rolls back if the event cannot be written); a repeated identical
    request changes and records nothing. ``actor_id`` names who made the
    change (an executive ID or a ``system:`` operator path).
    """

    async def register_executive(
        self, registration: ExecutiveRegistration, *, actor_id: str = SYSTEM_ACTOR
    ) -> ExecutiveAccess:
        """Create, or update roles/label of the same identity."""
        ...

    async def replace_products(
        self,
        executive_id: str,
        product_ids: Iterable[str],
        *,
        actor_id: str = SYSTEM_ACTOR,
    ) -> ExecutiveAccess:
        """Make the given set the executive's complete product entitlement."""
        ...

    async def set_active(
        self, executive_id: str, active: bool, *, actor_id: str = SYSTEM_ACTOR
    ) -> ExecutiveAccess: ...


class SessionLookup(Protocol):
    async def get_session(self, session_id: str) -> Session | None: ...


class RunLookup(Protocol):
    async def get_run(self, run_id: str) -> Run | None: ...


class OperationLookup(Protocol):
    async def get(self, operation_id: str) -> ToolExecution | None: ...
