"""In-memory report repository obeying the PostgreSQL repository's contract."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.reports import NewReportVersion
from retail_analytics.domain.reports import (
    ReportError,
    ReportErrorCode,
    ReportVersion,
)


@dataclass
class FakeReportRepository:
    clock: Callable[[], datetime]
    rows: list[ReportVersion] = field(default_factory=list)
    keys: dict[tuple[str, str], ReportVersion] = field(default_factory=dict)
    sessions: dict[str, str | None] = field(default_factory=dict)
    deleted: set[str] = field(default_factory=set)

    async def find_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> ReportVersion | None:
        return self.keys.get((owner_id, idempotency_key))

    async def add_version(self, new: NewReportVersion) -> tuple[ReportVersion, bool]:
        if (found := self.keys.get((new.owner_id, new.idempotency_key))) is not None:
            return found, False
        mine = [r for r in self.rows if r.report_id == new.report_id]
        if mine and (mine[0].owner_id != new.owner_id or new.report_id in self.deleted):
            raise AccessDenied("report", new.report_id)
        if len(mine) != new.expected_latest:
            raise ReportError(ReportErrorCode.STALE_BASE_VERSION, "stale")
        self.sessions.setdefault(new.report_id, new.session_id)
        row = ReportVersion(
            report_id=new.report_id,
            version=len(mine) + 1,
            owner_id=new.owner_id,
            session_id=self.sessions[new.report_id],
            run_id=new.run_id,
            artifact_version=new.artifact_version,
            title=new.title,
            evidence_ids=new.evidence_ids,
            scope_digest=new.scope_digest,
            authorization_version=new.authorization_version,
            draft_digest=new.draft_digest,
            created_at=self.clock(),
        )
        self.rows.append(row)
        self.keys[(new.owner_id, new.idempotency_key)] = row
        return row, True

    def _live(self, owner_id: str, report_id: str) -> list[ReportVersion]:
        return [
            r
            for r in self.rows
            if r.report_id == report_id
            and r.owner_id == owner_id
            and report_id not in self.deleted
        ]

    async def get(
        self, owner_id: str, report_id: str, version: int | None = None
    ) -> ReportVersion | None:
        mine = self._live(owner_id, report_id)
        if version is not None:
            return next((r for r in mine if r.version == version), None)
        return mine[-1] if mine else None

    async def versions(self, owner_id: str, report_id: str) -> Sequence[ReportVersion]:
        return self._live(owner_id, report_id)

    async def latest(
        self,
        owner_id: str,
        *,
        session_id: str | None = None,
        limit: int,
        offset: int = 0,
    ) -> Sequence[ReportVersion]:
        ids = dict.fromkeys(r.report_id for r in self.rows if r.owner_id == owner_id)
        latest = [
            self._live(owner_id, i)[-1]
            for i in ids
            if self._live(owner_id, i)
            and (session_id is None or self.sessions[i] == session_id)
        ]
        latest.sort(key=lambda r: r.created_at, reverse=True)
        return latest[offset : offset + limit]
