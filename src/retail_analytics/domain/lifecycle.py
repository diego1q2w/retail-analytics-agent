"""Retention and recovery rules for reports, investigations and audits.

Three obligations are kept apart:

* a **soft-deleted report** is recoverable by hand until ``deleted_at`` plus
  ``RECOVERY_PERIOD`` and is purged afterwards;
* an **investigation** (messages, progress, unpinned evidence) is kept for
  seven days after the later of the last user interaction and the last run
  completion;
* **evidence** that a saved report pins or cites, or that retained evidence
  derives from, survives investigation expiry; external operations that are
  not resolved keep the records needed to reconcile them.

Everything here is pure: the stores apply these rules under row locks.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from retail_analytics.domain.report_deletion import RECOVERY_PERIOD

INVESTIGATION_RETENTION = timedelta(days=7)
DEFAULT_AUDIT_RETENTION_DAYS = 90
DEFAULT_BATCH_SIZE = 100
# An unresolved operation older than this is flagged for manual resolution
# instead of being polled for ever.
UNRESOLVED_FLAG_AFTER = timedelta(hours=24)

RESTORED = "report.restored"
PURGED = "report.purged"
REUSE_REVALIDATED = "report.reuse_revalidated"
UNRESOLVED_FLAGGED = "maintenance.unresolved_flagged"
SYSTEM_ACTOR = "system:maintenance"


class RestoreErrorCode(StrEnum):
    # Unknown report, or one the actor may not restore (same answer for both).
    NOT_FOUND = "not_found"
    NOT_DELETED = "not_deleted"
    WINDOW_CLOSED = "window_closed"
    PURGED = "purged"
    OWNER_UNAVAILABLE = "owner_unavailable"


class RestoreError(Exception):
    """A restore was refused. ``message`` is safe to show the caller."""

    def __init__(self, code: RestoreErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


def recoverable_until(deleted_at: datetime) -> datetime:
    return deleted_at + RECOVERY_PERIOD


def is_restorable(deleted_at: datetime, now: datetime) -> bool:
    """Restore works strictly before the deadline; purge starts at it."""
    return now < recoverable_until(deleted_at)


def is_purgeable(deleted_at: datetime, now: datetime) -> bool:
    return now >= recoverable_until(deleted_at)


def investigation_expires_at(
    last_activity_at: datetime, last_run_completed_at: datetime | None
) -> datetime:
    """Seven days after the later of last interaction and last run completion.

    Background progress does not extend it: only ``last_activity_at`` (user
    interaction) and run completion count.
    """
    latest = last_activity_at
    if last_run_completed_at is not None and last_run_completed_at > latest:
        latest = last_run_completed_at
    return latest + INVESTIGATION_RETENTION


def removable_evidence(
    session_evidence: Collection[str],
    *,
    pinned: Collection[str],
    cited: Collection[str],
    dependencies: Mapping[str, Iterable[str]],
    external_dependents: Collection[str] = (),
) -> frozenset[str]:
    """Which of an expired investigation's evidence may be deleted.

    ``pinned`` and ``cited`` evidence (report holds and citations) is kept, and
    so is everything retained evidence is derived from, however deep.
    ``dependencies`` maps an evidence ID to the IDs it was computed from and
    must cover every evidence that depends on the session's evidence.
    ``external_dependents`` are session evidence IDs that evidence outside the
    session depends on; they are roots too.
    """
    session = set(session_evidence)
    keep: set[str] = (set(pinned) | set(cited) | set(external_dependents)) & session
    frontier = list(keep)
    while frontier:
        for source in dependencies.get(frontier.pop(), ()):
            if source in session and source not in keep:
                keep.add(source)
                frontier.append(source)
    return frozenset(session - keep)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Operator-tunable retention. Recovery and investigation periods are fixed."""

    audit_retention: timedelta = timedelta(days=DEFAULT_AUDIT_RETENTION_DAYS)
    batch_size: int = DEFAULT_BATCH_SIZE
    unresolved_flag_after: timedelta = UNRESOLVED_FLAG_AFTER

    def __post_init__(self) -> None:
        if self.audit_retention <= timedelta(0):
            raise ValueError("audit_retention must be positive")
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")
