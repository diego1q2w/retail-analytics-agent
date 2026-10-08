"""Calendar periods and comparison conventions.

Conventions (contract §6, design §37): UTC calendar dates, half-open windows
``[start, end)``, "last month" is the previous full calendar month, and an
incomplete period is labelled and compared with an equivalent elapsed period.

Only whole days that have finished count as observed: the current day is partial,
so the effective end of a still-running period is ``as_of`` (exclusive). Nothing
here silently moves to another period; every shortened or shifted bound is
reported on the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import StrEnum

DEFAULT_TIME_ZONE = "UTC"


class PeriodOrigin(StrEnum):
    DEFAULT = "default"
    USER_OVERRIDE = "user_override"


class OverrideScope(StrEnum):
    """How far a user's explicit period choice reaches (narrowest first)."""

    REPORT = "report"
    SESSION = "session"
    USER_DEFAULT = "user_default"


@dataclass(frozen=True, slots=True)
class DateWindow:
    """Half-open window of calendar dates, ``start`` inclusive, ``end`` exclusive."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("window end must not precede start")

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def is_empty(self) -> bool:
        return self.end == self.start

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end

    @property
    def last_day(self) -> date:
        """Last included day, for display (the window itself stays half-open)."""
        return self.end - timedelta(days=1)

    def describe(self) -> str:
        if self.is_empty:
            return f"empty window at {self.start.isoformat()}"
        return f"{self.start.isoformat()} to {self.last_day.isoformat()} inclusive"


def as_of_date(now: datetime, zone: tzinfo = UTC) -> date:
    """Calendar date of ``now`` in the analysis time zone (UTC by default)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(zone).date()


def month_window(year: int, month: int) -> DateWindow:
    start = date(year, month, 1)
    return DateWindow(start, _next_month_start(start))


def previous_full_month(as_of: date) -> DateWindow:
    """The previous complete calendar month relative to ``as_of``."""
    first_of_this = as_of.replace(day=1)
    last_of_previous = first_of_this - timedelta(days=1)
    return month_window(last_of_previous.year, last_of_previous.month)


def _next_month_start(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def _previous_month_start(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _is_calendar_month(window: DateWindow) -> bool:
    return window.start.day == 1 and window.end == _next_month_start(window.start)


@dataclass(frozen=True, slots=True)
class ResolvedPeriod:
    """A requested period together with what is actually observable."""

    requested: DateWindow
    effective: DateWindow
    as_of: date
    origin: PeriodOrigin = PeriodOrigin.DEFAULT
    override_scope: OverrideScope | None = None

    def __post_init__(self) -> None:
        if (self.origin is PeriodOrigin.USER_OVERRIDE) != (
            self.override_scope is not None
        ):
            raise ValueError("override scope is required exactly for user overrides")
        if not (
            self.requested.start == self.effective.start or self.effective.is_empty
        ):
            raise ValueError("effective window must start where the request starts")
        if self.effective.end > self.requested.end:
            raise ValueError("effective window cannot exceed the requested window")

    @property
    def is_complete(self) -> bool:
        return self.effective == self.requested

    @property
    def is_empty(self) -> bool:
        return self.effective.is_empty

    @property
    def elapsed_days(self) -> int:
        return self.effective.days

    def label(self) -> str:
        if self.is_complete:
            return f"{self.requested.describe()} (complete period)"
        return (
            f"INCOMPLETE period: requested {self.requested.describe()}; "
            f"data covers {self.effective.describe()} "
            f"({self.elapsed_days} of {self.requested.days} days)"
        )


def resolve_period(
    requested: DateWindow,
    as_of: date,
    *,
    origin: PeriodOrigin = PeriodOrigin.DEFAULT,
    override_scope: OverrideScope | None = None,
) -> ResolvedPeriod:
    """Clip ``requested`` to complete days before ``as_of``, keeping the request.

    A user-chosen window keeps its own bounds and scope; it is never replaced by
    a default, only reported as incomplete when it extends past ``as_of``.
    """
    end = min(requested.end, max(as_of, requested.start))
    return ResolvedPeriod(
        requested=requested,
        effective=DateWindow(requested.start, end),
        as_of=as_of,
        origin=origin,
        override_scope=override_scope,
    )


def default_period(as_of: date) -> ResolvedPeriod:
    """Default analysis period: the previous full calendar month."""
    return resolve_period(previous_full_month(as_of), as_of)


@dataclass(frozen=True, slots=True)
class PeriodComparison:
    """Current period against its equivalent elapsed prior period."""

    current: ResolvedPeriod
    prior: DateWindow
    prior_requested: DateWindow
    like_for_like: bool
    note: str

    def describe(self) -> str:
        return (
            f"{self.current.label()} compared with {self.prior.describe()}. {self.note}"
        )


def equivalent_prior_period(current: ResolvedPeriod) -> PeriodComparison:
    """Prior period covering the same number of elapsed days.

    Calendar month: the preceding month, truncated to the days elapsed when the
    current one is incomplete (capped at the prior month's length); a complete
    month is compared with the whole prior month. Unequal lengths are flagged as
    not like-for-like. Any other window: the immediately preceding window of equal
    length. An empty current period has nothing to compare.
    """
    if current.is_empty:
        empty = DateWindow(current.requested.start, current.requested.start)
        return PeriodComparison(
            current, empty, empty, False, "No complete days observed yet."
        )
    elapsed = current.elapsed_days
    if _is_calendar_month(current.requested):
        prior_start = _previous_month_start(current.requested.start)
        prior_full = month_window(prior_start.year, prior_start.month)
        length = (
            prior_full.days if current.is_complete else min(elapsed, prior_full.days)
        )
        prior = DateWindow(prior_start, prior_start + timedelta(days=length))
        like = length == elapsed
        if like:
            note = "Same number of days."
        else:
            note = (
                f"The prior month has {length} days against {elapsed} days in the "
                "current period: not like-for-like."
            )
        return PeriodComparison(current, prior, prior_full, like, note)
    requested_len = current.requested.days
    prior_requested = DateWindow(
        current.requested.start - timedelta(days=requested_len),
        current.requested.start,
    )
    prior = DateWindow(
        prior_requested.start, prior_requested.start + timedelta(days=elapsed)
    )
    return PeriodComparison(
        current, prior, prior_requested, True, "Same number of elapsed days."
    )
