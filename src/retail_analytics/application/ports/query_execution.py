from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.query_compiler import CompiledQuery
from retail_analytics.application.contracts.query_execution import QueryAuthority
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.contracts.warehouse_jobs import JobStatistics


class AuthorityProvider(Protocol):
    async def resolve(
        self, principal: Principal, run_id: str, *, trace_id: str | None = None
    ) -> QueryAuthority:
        """Raises ``AccessDenied`` or ``CatalogUnavailable``."""
        ...


class QueryAdmission(Protocol):
    """Decides, before a job reference is recorded, whether it may be submitted.

    Called once per new submission (operation ID, submission number) with the
    dry-run estimate; a retried attempt may call it again for the same pair,
    so implementations must be idempotent on it. Raises ``QueryNotAdmitted``.
    """

    async def admit(
        self,
        context: ExecutionContext,
        operation_id: str,
        submission: int,
        compiled: CompiledQuery,
        estimated_bytes: int,
    ) -> None: ...


class QueryUsageRecorder(Protocol):
    """Settles the actual usage of a finished job (idempotent, any number of
    calls per (operation, submission))."""

    async def settle(
        self,
        run_id: str,
        operation_id: str,
        submission: int,
        statistics: JobStatistics,
    ) -> None: ...
