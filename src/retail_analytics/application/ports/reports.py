from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.application.contracts.reports import NewReportVersion
from retail_analytics.domain.reports import ReportVersion


class ReportRepository(Protocol):
    """Report ownership, versions and evidence links (PostgreSQL).

    Every read takes the owner and returns only that owner's live (not
    soft-deleted) reports; another owner's report is indistinguishable from a
    missing one.
    """

    async def find_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> ReportVersion | None: ...

    async def add_version(self, new: NewReportVersion) -> tuple[ReportVersion, bool]:
        """Append a version, creating the report for ``expected_latest == 0``.

        Serialized per report. An existing (owner, key) returns the original
        with ``False``; a newly stored version returns ``True``.
        Raises ``AccessDenied`` for another owner's or a deleted report and
        ``ReportError`` (stale base version) when the latest differs.
        """
        ...

    async def get(
        self, owner_id: str, report_id: str, version: int | None = None
    ) -> ReportVersion | None:
        """The version, or the latest when ``version`` is None."""
        ...

    async def versions(
        self, owner_id: str, report_id: str
    ) -> Sequence[ReportVersion]: ...

    async def latest(
        self,
        owner_id: str,
        *,
        session_id: str | None = None,
        limit: int,
        offset: int = 0,
    ) -> Sequence[ReportVersion]:
        """Latest version of each live report, newest first."""
        ...
