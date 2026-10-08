"""Server-side authorization: current entitlements, ownership and context.

Every use case and every retried activity builds its ``ExecutionContext``
through ``AccessResolver``, which reloads the executive's roles, products and
authorization version from the directory and checks that the run belongs to
the caller. Nothing here is cached, so an entitlement change is visible to the
next tool check. Inaccessible and missing records raise the same
``AccessDenied``, so callers cannot probe for other executives' records.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from retail_analytics.application.contracts import Correlation
from retail_analytics.application.tools.context import ExecutionContext
from retail_analytics.domain.access import ExecutiveAccess, Permission, Role
from retail_analytics.domain.conversation import Session
from retail_analytics.domain.executions import ToolExecution
from retail_analytics.domain.runs import Run


class AccessDenied(Exception):
    """Not found or not accessible to this executive (deliberately the same)."""

    def __init__(self, kind: str, key: str) -> None:
        self.kind = kind
        self.key = key
        super().__init__(f"{kind} {key!r} is not available")


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


class ExecutiveDirectory(Protocol):
    """Read side: current authority of executives."""

    async def find_by_subject(
        self, issuer: str, subject: str
    ) -> ExecutiveAccess | None: ...

    async def get(self, executive_id: str) -> ExecutiveAccess | None: ...


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


class AccessAdministration(Protocol):
    """Write side, for trusted operator paths only (never a model tool).

    Every effective change increments the executive's authorization version
    in the same transaction; a repeated identical request changes nothing.
    """

    async def register_executive(
        self, registration: ExecutiveRegistration
    ) -> ExecutiveAccess:
        """Create, or update roles/label of the same identity."""
        ...

    async def replace_products(
        self, executive_id: str, product_ids: Iterable[str]
    ) -> ExecutiveAccess:
        """Make the given set the executive's complete product entitlement."""
        ...

    async def set_active(self, executive_id: str, active: bool) -> ExecutiveAccess: ...


def require_owner(kind: str, key: str, owner_id: str, executive_id: str) -> None:
    """Reusable ownership rule for any owned record (sessions, reports, ...)."""
    if not executive_id or owner_id != executive_id:
        raise AccessDenied(kind, key)


class SessionLookup(Protocol):
    async def get_session(self, session_id: str) -> Session | None: ...


class RunLookup(Protocol):
    async def get_run(self, run_id: str) -> Run | None: ...


class OperationLookup(Protocol):
    async def get(self, operation_id: str) -> ToolExecution | None: ...


class OwnershipGuard:
    """Loads records only for the executive who owns them.

    A run must be requested by the executive *and* sit in their session; an
    operation inherits its run's ownership. Satisfied by the persistence
    repositories.
    """

    def __init__(
        self,
        sessions: SessionLookup,
        runs: RunLookup,
        operations: OperationLookup,
    ) -> None:
        self._sessions = sessions
        self._runs = runs
        self._operations = operations

    async def session(self, executive_id: str, session_id: str) -> Session:
        session = await self._sessions.get_session(session_id)
        if session is None:
            raise AccessDenied("session", session_id)
        require_owner("session", session_id, session.executive_id, executive_id)
        return session

    async def run(self, executive_id: str, run_id: str) -> Run:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AccessDenied("run", run_id)
        require_owner("run", run_id, run.requested_by, executive_id)
        try:
            await self.session(executive_id, run.session_id)
        except AccessDenied:
            raise AccessDenied("run", run_id) from None
        return run

    async def operation(self, executive_id: str, operation_id: str) -> ToolExecution:
        execution = await self._operations.get(operation_id)
        if execution is None:
            raise AccessDenied("operation", operation_id)
        try:
            await self.run(executive_id, execution.run_id)
        except AccessDenied:
            raise AccessDenied("operation", operation_id) from None
        return execution


class AccessResolver:
    """Builds the trusted context tools run under, from current state only."""

    def __init__(self, directory: ExecutiveDirectory, guard: OwnershipGuard) -> None:
        self._directory = directory
        self._guard = guard

    async def current_access(self, principal: Principal) -> ExecutiveAccess:
        access = await self._directory.get(principal.executive_id)
        if access is None or not access.active:
            raise AccessDenied("executive", principal.executive_id)
        return access

    async def effective_permissions(self, principal: Principal) -> frozenset[str]:
        access = await self.current_access(principal)
        return frozenset(access.permissions) & principal.scopes

    async def require_permission(
        self, principal: Principal, permission: Permission
    ) -> ExecutiveAccess:
        access = await self.current_access(principal)
        if permission not in access.permissions or permission not in principal.scopes:
            raise AccessDenied("permission", permission.value)
        return access

    async def context_for_run(
        self, principal: Principal, run_id: str, *, trace_id: str | None = None
    ) -> ExecutionContext:
        """Fresh context for work in ``run_id``; call again on every attempt."""
        run = await self._guard.run(principal.executive_id, run_id)
        access = await self.current_access(principal)
        return ExecutionContext(
            executive_id=access.executive_id,
            permissions=frozenset(access.permissions) & principal.scopes,
            product_scope=access.product_scope,
            correlation=Correlation(
                session_id=run.session_id, run_id=run.run_id, trace_id=trace_id
            ),
        )
