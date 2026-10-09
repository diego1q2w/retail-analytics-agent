"""Pure retention rules: deadlines and which evidence survives expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from retail_analytics.domain.lifecycle import (
    INVESTIGATION_RETENTION,
    investigation_expires_at,
    is_purgeable,
    is_restorable,
    recoverable_until,
    removable_evidence,
)
from retail_analytics.domain.report_deletion import RECOVERY_PERIOD

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_recovery_deadline_is_seven_days_and_restore_purge_never_overlap() -> None:
    assert timedelta(days=7) == RECOVERY_PERIOD
    deadline = recoverable_until(T0)
    assert deadline == T0 + timedelta(days=7)
    just_before = deadline - timedelta(microseconds=1)
    assert is_restorable(T0, just_before) and not is_purgeable(T0, just_before)
    assert not is_restorable(T0, deadline) and is_purgeable(T0, deadline)


def test_investigation_expires_seven_days_after_the_later_signal() -> None:
    assert timedelta(days=7) == INVESTIGATION_RETENTION
    assert investigation_expires_at(T0, None) == T0 + timedelta(days=7)
    later = T0 + timedelta(days=2)
    assert investigation_expires_at(T0, later) == later + timedelta(days=7)
    # A run that completed before the last interaction does not matter.
    assert investigation_expires_at(later, T0) == later + timedelta(days=7)


def test_unpinned_independent_evidence_is_removable() -> None:
    assert removable_evidence(["a", "b"], pinned=[], cited=[], dependencies={}) == {
        "a",
        "b",
    }


def test_pinned_cited_and_their_inputs_survive() -> None:
    deps = {"c": ["b"], "b": ["a"], "e": ["d"]}
    out = removable_evidence(
        ["a", "b", "c", "d", "e", "f"], pinned=["c"], cited=["f"], dependencies=deps
    )
    assert out == {"d", "e"}


def test_evidence_used_by_evidence_outside_the_session_survives_with_inputs() -> None:
    out = removable_evidence(
        ["a", "b", "c"],
        pinned=[],
        cited=[],
        dependencies={"b": ["a"]},
        external_dependents=["b"],
    )
    assert out == {"c"}


def test_inputs_from_other_sessions_are_not_decided_here() -> None:
    out = removable_evidence(
        ["a"], pinned=["a"], cited=[], dependencies={"a": ["elsewhere"]}
    )
    assert out == frozenset()
