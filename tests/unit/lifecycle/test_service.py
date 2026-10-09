"""Recovery authorization and the cleanup sequence, with an in-memory store."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from retail_analytics.application.artifacts import ArtifactMaintenance, ReconcileReport
from retail_analytics.application.authorization import AccessDenied, AccessResolver
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.lifecycle import (
    ContentRemoval,
    InvestigationCleanup,
    MaintenanceLimits,
    PreviewCounts,
    RestorableReport,
    RestoredReport,
    UnresolvedOperation,
)
from retail_analytics.application.lifecycle import LifecycleService
from retail_analytics.domain.access import Permission, Role
from retail_analytics.domain.lifecycle import RetentionPolicy
from tests.unit.test_authorization import ALL_SCOPES, Directory, Records, access

NOW = datetime(2026, 10, 9, tzinfo=UTC)


@dataclass
class FakeStore:
    due: list[str] = field(default_factory=list)
    removal: dict[str, ContentRemoval] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    restores: list[tuple[str, str, bool]] = field(default_factory=list)
    previews: int = 0
    audit_deleted: int = 3

    async def restore_report(
        self,
        *,
        report_id: str,
        actor_id: str,
        actor_is_admin: bool,
        at: datetime,
        audit_id: str,
    ) -> RestoredReport:
        self.restores.append((report_id, actor_id, actor_is_admin))
        return RestoredReport(report_id, "owner", at)

    async def list_restorable(
        self, owner_id: str, at: datetime
    ) -> Sequence[RestorableReport]:
        return ()

    async def due_reports(self, at: datetime, limit: int) -> Sequence[str]:
        return self.due[:limit]

    async def remove_report_content(
        self, report_id: str, at: datetime
    ) -> ContentRemoval:
        self.events.append(f"remove:{report_id}")
        return self.removal.get(report_id, ContentRemoval.REMOVED)

    async def finish_report_purge(
        self, report_id: str, at: datetime, audit_id: str
    ) -> bool:
        self.events.append(f"finish:{report_id}")
        return True

    async def purge_expired_investigations(
        self, at: datetime, *, limit: int
    ) -> InvestigationCleanup:
        self.events.append("investigations")
        return InvestigationCleanup(sessions_scanned=1, sessions_removed=1)

    async def delete_audit_before(self, cutoff: datetime, limit: int) -> int:
        self.events.append(f"audit<{cutoff.date()}")
        return self.audit_deleted

    async def flag_unresolved(
        self, older_than: datetime, at: datetime, *, limit: int
    ) -> int:
        self.events.append("flag")
        return 2

    async def unresolved_operations(self, limit: int) -> Sequence[UnresolvedOperation]:
        return ()

    async def preview(
        self, at: datetime, *, audit_cutoff: datetime, flag_after: timedelta
    ) -> PreviewCounts:
        self.previews += 1
        return PreviewCounts(4, 5, 6, 7)


class FakeMaintenance(ArtifactMaintenance):
    def __init__(self, store: FakeStore, fail: set[str] | None = None) -> None:
        self.store = store
        self.fail = fail or set()

    async def purge(self, artifact_id: str) -> int:
        self.store.events.append(f"artifact:{artifact_id}")
        if artifact_id in self.fail:
            raise OSError("disk")
        return 1

    async def reconcile(self, grace: timedelta = timedelta(hours=1)) -> ReconcileReport:
        self.store.events.append("reconcile")
        return ReconcileReport(1, 2, ("k",))


def service(
    store: FakeStore,
    *,
    roles: frozenset[Role] = frozenset({Role.EXECUTIVE}),
    fail: set[str] | None = None,
    policy: RetentionPolicy | None = None,
) -> LifecycleService:
    directory = Directory()
    directory.by_id = {
        "owner": access("owner", {"1"}, roles=roles),
        "admin": access("admin", set(), roles=frozenset({Role.ADMIN})),
        "editor": access("editor", set(), roles=frozenset({Role.EDITOR})),
    }
    records = Records()
    from retail_analytics.application.authorization import OwnershipGuard

    resolver = AccessResolver(directory, OwnershipGuard(records, records, records))
    return LifecycleService(
        store,
        resolver,
        FakeMaintenance(store, fail),
        policy=policy,
        clock=lambda: NOW,
        new_id=lambda: "audit-id",
    )


def who(executive_id: str) -> Principal:
    return Principal(executive_id, ALL_SCOPES)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_owner_restores_and_admin_flag_is_passed_through() -> None:
    store = FakeStore()
    svc = service(store)
    await svc.restore(who("owner"), "r1")
    assert store.restores == [("r1", "owner", False)]
    await svc.restore(who("admin"), "r1")
    assert store.restores[-1] == ("r1", "admin", True)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_principal_without_delete_or_admin_permission_cannot_restore() -> None:
    store = FakeStore()
    with pytest.raises(AccessDenied):
        await service(store).restore(who("editor"), "r1")
    # A token scope can only narrow: no delete scope, no restore.
    narrowed = Principal("owner", frozenset({Permission.ANALYSIS_READ.value}))
    with pytest.raises(AccessDenied):
        await service(store).restore(narrowed, "r1")
    assert store.restores == []


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_report_purge_removes_content_then_artifact_then_tombstone() -> None:
    store = FakeStore(due=["r1", "r2"])
    report = await service(store).run_maintenance()
    assert store.events[:6] == [
        "remove:r1",
        "artifact:r1",
        "finish:r1",
        "remove:r2",
        "artifact:r2",
        "finish:r2",
    ]
    assert report.reports_purged == 2
    assert report.unresolved_flagged == 2
    assert report.audit_rows_removed == 3
    assert report.investigations.sessions_removed == 1
    assert (
        report.artifact_partials_removed,
        report.artifact_orphans_removed,
        report.artifact_missing_content,
    ) == (1, 2, 1)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_artifact_failure_keeps_the_tombstone_and_the_run_continues() -> None:
    store = FakeStore(due=["bad", "good"])
    report = await service(store, fail={"bad"}).run_maintenance()
    assert "finish:bad" not in store.events
    assert "finish:good" in store.events
    assert (report.reports_purged, report.reports_failed) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_blocked_and_skipped_reports_are_not_purged() -> None:
    store = FakeStore(
        due=["blocked", "skipped"],
        removal={
            "blocked": ContentRemoval.BLOCKED,
            "skipped": ContentRemoval.SKIPPED,
        },
    )
    report = await service(store).run_maintenance()
    assert (report.reports_purged, report.reports_blocked) == (0, 1)
    assert not any(e.startswith(("artifact:", "finish:")) for e in store.events)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_run_is_bounded_and_says_when_more_remain() -> None:
    store = FakeStore(due=[f"r{i}" for i in range(5)])
    report = await service(store).run_maintenance(MaintenanceLimits(max_reports=2))
    assert report.reports_purged == 2 and report.more_pending
    assert [e for e in store.events if e.startswith("finish:")] == [
        "finish:r0",
        "finish:r1",
    ]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_audit_cutoff_uses_the_configured_retention() -> None:
    store = FakeStore()
    await service(
        store, policy=RetentionPolicy(audit_retention=timedelta(days=30))
    ).run_maintenance()
    assert f"audit<{(NOW - timedelta(days=30)).date()}" in store.events


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_dry_run_reports_counts_and_changes_nothing() -> None:
    store = FakeStore(due=["r1"])
    report = await service(store).run_maintenance(dry_run=True)
    assert report.dry_run
    assert (
        report.reports_purged,
        report.investigations.sessions_removed,
        report.audit_rows_removed,
        report.unresolved_flagged,
    ) == (4, 5, 6, 7)
    assert store.events == [] and store.previews == 1


def test_restore_is_not_exposed_as_a_model_capability() -> None:
    from pathlib import Path

    import retail_analytics.capabilities as capabilities_pkg

    root = Path(capabilities_pkg.__file__).parent
    for source in root.rglob("*.py"):
        text = source.read_text()
        assert "LifecycleService" not in text, source
        assert "restore_report" not in text, source
