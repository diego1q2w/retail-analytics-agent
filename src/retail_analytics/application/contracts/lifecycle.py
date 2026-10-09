"""Values exchanged with report recovery and lifecycle cleanup.

Nothing here carries report titles, text or other content: restore results and
cleanup reports hold identifiers, timestamps and counts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from retail_analytics.domain.lifecycle import DEFAULT_BATCH_SIZE


@dataclass(frozen=True, slots=True)
class RestoredReport:
    report_id: str
    owner_id: str
    restored_at: datetime


@dataclass(frozen=True, slots=True)
class RestorableReport:
    """A soft-deleted report still inside its recovery period (no content)."""

    report_id: str
    created_at: datetime
    deleted_at: datetime
    recoverable_until: datetime


class ContentRemoval(StrEnum):
    # Content and holds were removed (now or by an earlier interrupted run).
    REMOVED = "removed"
    # Restored, not yet due, or already fully purged: nothing to do.
    SKIPPED = "skipped"
    # Unresolved operations or jobs still need this report's records.
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class UnresolvedOperation:
    operation_id: str
    run_id: str
    capability: str
    status: str
    since: datetime


@dataclass(frozen=True, slots=True)
class InvestigationCleanup:
    sessions_scanned: int = 0
    sessions_removed: int = 0
    # Expired sessions kept as a minimal shell because a saved report pins
    # their evidence; their messages, progress and unpinned evidence are gone.
    sessions_shell_kept: int = 0
    sessions_held_unresolved: int = 0
    sessions_held_active: int = 0
    evidence_removed: int = 0

    def __add__(self, other: InvestigationCleanup) -> InvestigationCleanup:
        return InvestigationCleanup(
            self.sessions_scanned + other.sessions_scanned,
            self.sessions_removed + other.sessions_removed,
            self.sessions_shell_kept + other.sessions_shell_kept,
            self.sessions_held_unresolved + other.sessions_held_unresolved,
            self.sessions_held_active + other.sessions_held_active,
            self.evidence_removed + other.evidence_removed,
        )


@dataclass(frozen=True, slots=True)
class PreviewCounts:
    """What a run could do right now (dry run); counts only."""

    reports_due: int
    investigations_expired: int
    audit_rows_due: int
    unresolved_to_flag: int


@dataclass(frozen=True, slots=True)
class MaintenanceLimits:
    """Upper bounds for one run; a run never does unbounded work."""

    max_reports: int = DEFAULT_BATCH_SIZE
    max_sessions: int = DEFAULT_BATCH_SIZE
    max_audit_rows: int = 1000

    def __post_init__(self) -> None:
        if min(self.max_reports, self.max_sessions, self.max_audit_rows) < 1:
            raise ValueError("limits must be at least 1")


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """Counts only. In a dry run they say what a real run could do now."""

    dry_run: bool
    reports_purged: int = 0
    reports_blocked: int = 0
    reports_failed: int = 0
    investigations: InvestigationCleanup = InvestigationCleanup()
    audit_rows_removed: int = 0
    unresolved_flagged: int = 0
    artifact_partials_removed: int = 0
    artifact_orphans_removed: int = 0
    artifact_missing_content: int = 0
    # True when a bound stopped the run early: run again to continue.
    more_pending: bool = False
