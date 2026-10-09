"""PostgreSQL ``ReportRepository``: owner-scoped reads, serialized versioning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import report_evidence as re_
from retail_analytics.adapters.postgres.schema import report_versions as rv
from retail_analytics.adapters.postgres.schema import reports as rp
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.reports import NewReportVersion
from retail_analytics.domain.reports import (
    ReportError,
    ReportErrorCode,
    ReportVersion,
)


def _columns() -> list[sa.Column[Any]]:
    return [
        rv.c.report_id,
        rv.c.version,
        rv.c.owner_id,
        rv.c.artifact_version,
        rv.c.title,
        rv.c.run_id,
        rv.c.draft_digest,
        rv.c.scope_digest,
        rv.c.authorization_version,
        rv.c.created_at,
        rp.c.session_id,
    ]


def _version(m: Mapping[Any, Any], evidence: Sequence[str]) -> ReportVersion:
    return ReportVersion(
        report_id=m["report_id"],
        version=m["version"],
        owner_id=m["owner_id"],
        session_id=m["session_id"],
        run_id=m["run_id"],
        artifact_version=m["artifact_version"],
        title=m["title"],
        evidence_ids=tuple(evidence),
        scope_digest=m["scope_digest"],
        authorization_version=m["authorization_version"],
        draft_digest=m["draft_digest"],
        created_at=m["created_at"],
    )


def _join() -> sa.Join:
    return rv.join(
        rp, sa.and_(rp.c.report_id == rv.c.report_id, rp.c.owner_id == rv.c.owner_id)
    )


def _evidence_for(
    connection: sa.Connection, keys: Sequence[tuple[str, int]]
) -> dict[tuple[str, int], list[str]]:
    found: dict[tuple[str, int], list[str]] = {k: [] for k in keys}
    if not keys:
        return found
    rows = connection.execute(
        sa.select(re_.c.report_id, re_.c.version, re_.c.evidence_id)
        .where(sa.tuple_(re_.c.report_id, re_.c.version).in_(list(keys)))
        .order_by(re_.c.report_id, re_.c.version, re_.c.ordinal)
    )
    for report_id, version, evidence_id in rows:
        found[(report_id, version)].append(evidence_id)
    return found


def _hydrate(
    connection: sa.Connection, rows: Sequence[sa.Row[Any]]
) -> list[ReportVersion]:
    evidence = _evidence_for(connection, [(r.report_id, r.version) for r in rows])
    return [_version(r._mapping, evidence[(r.report_id, r.version)]) for r in rows]


class PostgresReportRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def find_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> ReportVersion | None:
        return await self._db.transaction(self._find, owner_id, idempotency_key)

    async def add_version(self, new: NewReportVersion) -> tuple[ReportVersion, bool]:
        return await self._db.transaction(self._add, new)

    async def get(
        self, owner_id: str, report_id: str, version: int | None = None
    ) -> ReportVersion | None:
        return await self._db.transaction(self._get, owner_id, report_id, version)

    async def versions(self, owner_id: str, report_id: str) -> Sequence[ReportVersion]:
        return await self._db.transaction(self._versions, owner_id, report_id)

    async def latest(
        self,
        owner_id: str,
        *,
        session_id: str | None = None,
        limit: int,
        offset: int = 0,
    ) -> Sequence[ReportVersion]:
        return await self._db.transaction(
            self._latest, owner_id, session_id, limit, offset
        )

    @staticmethod
    def _find(
        connection: sa.Connection, owner_id: str, key: str
    ) -> ReportVersion | None:
        row = connection.execute(
            sa.select(*_columns())
            .select_from(_join())
            .where(rv.c.owner_id == owner_id, rv.c.idempotency_key == key)
        ).one_or_none()
        return None if row is None else _hydrate(connection, [row])[0]

    def _add(
        self, connection: sa.Connection, new: NewReportVersion
    ) -> tuple[ReportVersion, bool]:
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtextextended(new.report_id, 0)
                )
            )
        )
        existing = self._find(connection, new.owner_id, new.idempotency_key)
        if existing is not None:
            return existing, False
        report = connection.execute(
            sa.select(rp.c.owner_id, rp.c.deleted_at).where(
                rp.c.report_id == new.report_id
            )
        ).one_or_none()
        now = self._db.clock()
        if report is None:
            connection.execute(
                insert(rp).values(
                    report_id=new.report_id,
                    owner_id=new.owner_id,
                    session_id=new.session_id,
                    created_at=now,
                )
            )
        elif report.owner_id != new.owner_id or report.deleted_at is not None:
            raise AccessDenied("report", new.report_id)
        latest = (
            connection.execute(
                sa.select(sa.func.max(rv.c.version)).where(
                    rv.c.report_id == new.report_id
                )
            ).scalar_one()
            or 0
        )
        if latest != new.expected_latest:
            raise ReportError(
                ReportErrorCode.STALE_BASE_VERSION,
                f"the report is at version {latest}",
            )
        connection.execute(
            insert(rv).values(
                report_id=new.report_id,
                version=latest + 1,
                owner_id=new.owner_id,
                artifact_version=new.artifact_version,
                title=new.title,
                run_id=new.run_id,
                idempotency_key=new.idempotency_key,
                draft_digest=new.draft_digest,
                scope_digest=new.scope_digest,
                authorization_version=new.authorization_version,
                created_at=now,
            )
        )
        connection.execute(
            insert(re_),
            [
                {
                    "report_id": new.report_id,
                    "version": latest + 1,
                    "evidence_id": evidence_id,
                    "ordinal": ordinal,
                }
                for ordinal, evidence_id in enumerate(new.evidence_ids)
            ],
        )
        stored = self._get(connection, new.owner_id, new.report_id, latest + 1)
        assert stored is not None  # noqa: S101
        return stored, True

    @staticmethod
    def _get(
        connection: sa.Connection,
        owner_id: str,
        report_id: str,
        version: int | None,
    ) -> ReportVersion | None:
        query = (
            sa.select(*_columns())
            .select_from(_join())
            .where(
                rv.c.report_id == report_id,
                rv.c.owner_id == owner_id,
                rp.c.deleted_at.is_(None),
            )
        )
        query = (
            query.where(rv.c.version == version)
            if version is not None
            else query.order_by(rv.c.version.desc()).limit(1)
        )
        row = connection.execute(query).one_or_none()
        return None if row is None else _hydrate(connection, [row])[0]

    @staticmethod
    def _versions(
        connection: sa.Connection, owner_id: str, report_id: str
    ) -> list[ReportVersion]:
        rows = connection.execute(
            sa.select(*_columns())
            .select_from(_join())
            .where(
                rv.c.report_id == report_id,
                rv.c.owner_id == owner_id,
                rp.c.deleted_at.is_(None),
            )
            .order_by(rv.c.version)
        ).all()
        return _hydrate(connection, rows)

    @staticmethod
    def _latest(
        connection: sa.Connection,
        owner_id: str,
        session_id: str | None,
        limit: int,
        offset: int,
    ) -> list[ReportVersion]:
        other = rv.alias("newer")
        newest = (
            sa.select(sa.func.max(other.c.version))
            .where(other.c.report_id == rp.c.report_id)
            .scalar_subquery()
        )
        query = (
            sa.select(*_columns())
            .select_from(_join())
            .where(
                rp.c.owner_id == owner_id,
                rp.c.deleted_at.is_(None),
                rv.c.version == newest,
            )
            .order_by(rv.c.created_at.desc(), rv.c.report_id)
            .limit(limit)
            .offset(offset)
        )
        if session_id is not None:
            query = query.where(rp.c.session_id == session_id)
        return _hydrate(connection, connection.execute(query).all())
