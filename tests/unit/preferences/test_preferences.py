"""Preference memory with scripted interactions (in-memory store)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
    OwnershipGuard,
    Principal,
)
from retail_analytics.application.preferences import PreferenceAction, PreferenceService
from retail_analytics.domain.access import Permission
from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.metric_preferences import resolve_term
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.periods import OverrideScope
from retail_analytics.domain.preferences import (
    ADAPT_THRESHOLD,
    PROPOSAL_TTL,
    EffectivePreferences,
    InvalidPreference,
    Preference,
    PreferenceKind,
    PreferenceSetting,
    PreferenceSource,
    parse_slot,
)
from tests.unit.preferences.fakes import (
    Clock,
    FakePreferenceStore,
    RecordingInvalidator,
)
from tests.unit.test_authorization import ALL_SCOPES, Directory, Records, access

CATALOG = default_catalog()
A = Principal("exec-a", ALL_SCOPES)
B = Principal("exec-b", ALL_SCOPES)
CURRENCY = PreferenceSetting(PreferenceKind.DISPLAY_CURRENCY, "EUR")
TABLE = PreferenceSetting(PreferenceKind.TABLE_FORMAT, "table")
LIST = PreferenceSetting(PreferenceKind.TABLE_FORMAT, "list")
_ALT = next(m for m in sorted(CATALOG.metric_ids()) if m != "completed_item_sales")
ALL_ITEM_SALES = PreferenceSetting.metric("revenue", _ALT, CATALOG.latest_version(_ALT))


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.store = FakePreferenceStore(clock=self.clock)
        self.invalidator = RecordingInvalidator()
        directory = Directory()
        directory.by_id = {
            "exec-a": access("exec-a", {"1"}),
            "exec-b": access("exec-b", {"2"}),
        }
        records = Records()
        for executive, suffix in (("exec-a", "a"), ("exec-a", "a2"), ("exec-b", "b")):
            records.add(executive, suffix)
        self.guard = OwnershipGuard(records, records, records)
        self.resolver = AccessResolver(directory, self.guard)
        self.directory = directory
        self.service = PreferenceService(
            self.store, self.resolver, self.guard, CATALOG, self.invalidator
        )


@pytest.fixture
def w() -> World:
    return World()


@pytest.mark.asyncio
async def test_explicit_preferences_survive_a_new_session_and_acknowledge_meaning(
    w: World,
) -> None:
    out = await w.service.remember(A, ALL_ITEM_SALES, session_id="s-a")
    assert out.action is PreferenceAction.REMEMBERED
    assert "'revenue' means" in out.message and _ALT in out.message
    assert "all future sessions" in out.message
    await w.service.remember(A, CURRENCY)
    await w.service.remember(A, TABLE)

    # A different session of the same executive sees the saved defaults.
    effective = await w.service.effective(A, session_id="s-a2")
    assert effective.value(PreferenceKind.DISPLAY_CURRENCY) == "EUR"
    assert effective.value(PreferenceKind.TABLE_FORMAT) == "table"
    resolved = resolve_term(
        CATALOG, "revenue", effective.definition_preferences(CATALOG)
    )
    assert resolved.definition.metric_id == _ALT
    assert "user_default" in resolved.source


@pytest.mark.asyncio
async def test_two_executives_never_see_each_others_memory(w: World) -> None:
    await w.service.remember(A, CURRENCY)
    await w.service.remember(
        B, PreferenceSetting(PreferenceKind.DISPLAY_CURRENCY, "USD")
    )
    assert (await w.service.effective(A)).value(
        PreferenceKind.DISPLAY_CURRENCY
    ) == "EUR"
    assert (await w.service.effective(B)).value(
        PreferenceKind.DISPLAY_CURRENCY
    ) == "USD"
    with pytest.raises(AccessDenied):  # B cannot use A's session
        await w.service.remember(
            B, TABLE, scope=OverrideScope.SESSION, session_id="s-a"
        )
    with pytest.raises(AccessDenied):
        await w.service.inspect(B, session_id="s-a")
    await w.service.forget(B, "display_currency")
    assert (await w.service.effective(A)).value(
        PreferenceKind.DISPLAY_CURRENCY
    ) == "EUR"


@pytest.mark.asyncio
async def test_precedence_report_over_session_over_default(w: World) -> None:
    await w.service.remember(A, LIST)
    await w.service.remember(A, TABLE, scope=OverrideScope.SESSION, session_id="s-a")
    in_session = await w.service.effective(A, session_id="s-a")
    other = await w.service.effective(A, session_id="s-a2")
    assert in_session.value(PreferenceKind.TABLE_FORMAT) == "table"
    assert other.value(PreferenceKind.TABLE_FORMAT) == "list"
    prose = PreferenceSetting(PreferenceKind.TABLE_FORMAT, "prose")
    report = await w.service.effective(A, session_id="s-a", temporary=(prose,))
    assert report.value(PreferenceKind.TABLE_FORMAT) == "prose"
    # Temporary instructions were not saved anywhere.
    assert (await w.service.effective(A, session_id="s-a")).value(
        PreferenceKind.TABLE_FORMAT
    ) == "table"
    with pytest.raises(InvalidPreference):
        await w.service.remember(A, prose, scope=OverrideScope.REPORT)


@pytest.mark.asyncio
async def test_temporary_metric_override_changes_resolution_only_for_that_report(
    w: World,
) -> None:
    effective = await w.service.effective(A, temporary=(ALL_ITEM_SALES,))
    prefs = effective.definition_preferences(CATALOG)
    assert resolve_term(CATALOG, "revenue", prefs).definition.metric_id == _ALT
    plain = await w.service.effective(A)
    assert resolve_term(
        CATALOG, "revenue", plain.definition_preferences(CATALOG)
    ).source == ("shared default")


@pytest.mark.asyncio
async def test_repeated_behaviour_adapts_session_but_never_persists(w: World) -> None:
    for _ in range(ADAPT_THRESHOLD - 1):
        out = await w.service.observe(A, "s-a", TABLE)
        assert out.action is PreferenceAction.OBSERVED
    out = await w.service.observe(A, "s-a", TABLE)
    assert out.action is PreferenceAction.PROPOSED and out.proposal_id
    assert "Should I remember" in out.message

    # The current session adapted, with an unconfirmed provenance...
    now = await w.service.effective(A, session_id="s-a")
    assert now.value(PreferenceKind.TABLE_FORMAT) == "table"
    # ...but another session and the defaults are untouched.
    assert (await w.service.effective(A, session_id="s-a2")).value(
        PreferenceKind.TABLE_FORMAT
    ) is None
    stored = await w.store.list_preferences("exec-a", None)
    assert stored == ()


@pytest.mark.asyncio
async def test_silence_and_time_never_confirm(w: World) -> None:
    for _ in range(ADAPT_THRESHOLD):
        out = await w.service.observe(A, "s-a", TABLE)
    w.clock.now += PROPOSAL_TTL + timedelta(days=30)
    assert (await w.service.effective(A, session_id="s-a2")).value(
        PreferenceKind.TABLE_FORMAT
    ) is None
    assert (await w.service.inspect(A)).proposals == ()
    with pytest.raises(InvalidTransition):  # expired proposals cannot be confirmed
        await w.service.confirm(A, out.proposal_id or "")


@pytest.mark.asyncio
async def test_confirmation_persists_as_inferred_confirmed(w: World) -> None:
    for _ in range(ADAPT_THRESHOLD):
        out = await w.service.observe(A, "s-a", TABLE)
    view = await w.service.inspect(A, session_id="s-a")
    assert [p.proposal_id for p in view.proposals] == [out.proposal_id]
    done = await w.service.confirm(A, out.proposal_id or "")
    assert done.action is PreferenceAction.CONFIRMED
    other = await w.service.effective(A, session_id="s-a2")
    assert other.value(PreferenceKind.TABLE_FORMAT) == "table"
    saved = next(
        p
        for p in await w.store.list_preferences("exec-a", None)
        if p.scope is OverrideScope.USER_DEFAULT
    )
    assert saved.source is PreferenceSource.INFERRED_CONFIRMED
    with pytest.raises(InvalidTransition):  # cannot be confirmed twice
        await w.service.confirm(A, out.proposal_id or "")


@pytest.mark.asyncio
async def test_declined_inference_is_not_asked_again_and_other_users_cannot_resolve(
    w: World,
) -> None:
    for _ in range(ADAPT_THRESHOLD):
        out = await w.service.observe(A, "s-a", TABLE)
    with pytest.raises(AccessDenied):
        await w.service.confirm(B, out.proposal_id or "")
    declined = await w.service.decline(A, out.proposal_id or "")
    assert declined.action is PreferenceAction.DECLINED
    assert await w.store.list_preferences("exec-a", None) == ()
    for _ in range(ADAPT_THRESHOLD + 2):  # new session, same behaviour
        again = await w.service.observe(A, "s-a2", TABLE)
        assert again.action is PreferenceAction.OBSERVED
    assert (await w.service.inspect(A)).proposals == ()


@pytest.mark.asyncio
async def test_inspect_change_forget(w: World) -> None:
    await w.service.remember(A, LIST)
    changed = await w.service.remember(A, TABLE)
    assert changed.action is PreferenceAction.CHANGED and changed.version == 2
    same = await w.service.remember(A, TABLE)
    assert same.action is PreferenceAction.UNCHANGED
    view = await w.service.inspect(A)
    assert [(p.setting.slot, p.setting.value, p.version) for p in view.preferences] == [
        ("table_format", "table", 2)
    ]
    gone = await w.service.forget(A, "table_format")
    assert gone.action is PreferenceAction.FORGOTTEN
    assert (await w.service.inspect(A)).preferences == ()
    assert (
        await w.service.forget(A, "table_format")
    ).action is PreferenceAction.NOT_FOUND
    assert [e.action for e in w.store.events] == ["remembered", "changed", "forgotten"]


@pytest.mark.asyncio
async def test_forget_everything_clears_defaults_and_pending_proposals(
    w: World,
) -> None:
    await w.service.remember(A, CURRENCY)
    await w.service.remember(B, CURRENCY)
    for _ in range(ADAPT_THRESHOLD):
        await w.service.observe(A, "s-a", TABLE)
    assert await w.service.forget_everything(A) == 2  # currency + adapted session row
    assert (await w.service.inspect(A, session_id="s-a")).proposals == ()
    assert len(await w.store.list_preferences("exec-b", None)) == 1


@pytest.mark.asyncio
async def test_metric_changes_invalidate_findings_but_formatting_does_not(
    w: World,
) -> None:
    await w.service.remember(A, TABLE)
    await w.service.remember(A, CURRENCY)
    await w.service.remember(A, LIST)
    assert w.invalidator.calls == []

    out = await w.service.remember(A, ALL_ITEM_SALES)
    assert out.invalidated_findings and "recalculated" in out.message
    assert w.invalidator.calls == [("exec-a", None, "metric_definition:revenue")]
    # Repeating the same preference changes nothing and invalidates nothing.
    await w.service.remember(A, ALL_ITEM_SALES)
    assert len(w.invalidator.calls) == 1
    # Forgetting a metric meaning changes results again.
    forgotten = await w.service.forget(A, "metric_definition:revenue")
    assert forgotten.invalidated_findings and len(w.invalidator.calls) == 2
    # A session-scoped change reaches only that session.
    await w.service.remember(
        A, ALL_ITEM_SALES, scope=OverrideScope.SESSION, session_id="s-a"
    )
    assert w.invalidator.calls[-1] == ("exec-a", "s-a", "metric_definition:revenue")


@pytest.mark.asyncio
async def test_fingerprint_changes_only_for_analytical_settings(w: World) -> None:
    base = await w.service.effective(A)
    await w.service.remember(A, TABLE)
    await w.service.remember(A, CURRENCY)
    formatted = await w.service.effective(A)
    assert not formatted.invalidates(base.analytical_fingerprint)
    await w.service.remember(A, ALL_ITEM_SALES)
    changed = await w.service.effective(A)
    assert changed.invalidates(base.analytical_fingerprint)
    await w.service.remember(
        A, PreferenceSetting(PreferenceKind.TIME_ZONE, "Europe/Madrid")
    )
    assert (await w.service.effective(A)).invalidates(changed.analytical_fingerprint)


@pytest.mark.asyncio
async def test_permissions_and_inactive_executives_are_enforced(w: World) -> None:
    narrow = Principal("exec-a", frozenset({Permission.REPORTS_READ_OWN.value}))
    with pytest.raises(AccessDenied):
        await w.service.remember(narrow, TABLE)
    w.directory.by_id["exec-a"] = access("exec-a", {"1"}, active=False)
    with pytest.raises(AccessDenied):
        await w.service.inspect(A)


@pytest.mark.asyncio
async def test_memory_cannot_widen_product_access_or_hold_raw_data() -> None:
    assert not any("product" in k.value or "scope" in k.value for k in PreferenceKind)
    for kind, value, term in (
        (PreferenceKind.TABLE_FORMAT, "product 1,2,3", None),
        (PreferenceKind.DISPLAY_CURRENCY, "eur", None),
        (PreferenceKind.TIME_ZONE, "Not/AZone", None),
        (PreferenceKind.METRIC_DEFINITION, "revenue; DROP", "revenue"),
        (PreferenceKind.METRIC_DEFINITION, "completed_item_sales@1", None),
        (PreferenceKind.DETAIL_LEVEL, "brief", "extra"),
    ):
        with pytest.raises(InvalidPreference):
            PreferenceSetting(kind, value, term)
    with pytest.raises(InvalidPreference):
        parse_slot("product_ids")
    # An unapproved or unknown metric version is rejected by the application.
    w = World()
    with pytest.raises(InvalidPreference):
        await w.service.remember(A, PreferenceSetting.metric("revenue", "churn", 1))
    with pytest.raises(InvalidPreference):
        await w.service.remember(A, PreferenceSetting.metric("revenue", _ALT, 99))


def test_unconfirmed_inference_cannot_be_a_default() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 8, tzinfo=UTC)
    with pytest.raises(InvalidPreference):
        Preference(
            "p",
            "e",
            TABLE,
            OverrideScope.USER_DEFAULT,
            None,
            PreferenceSource.INFERRED_SESSION,
            1,
            now,
            now,
        )
    with pytest.raises(InvalidPreference):
        Preference(
            "p",
            "e",
            TABLE,
            OverrideScope.REPORT,
            None,
            PreferenceSource.EXPLICIT,
            1,
            now,
            now,
        )


def test_stale_metric_preference_falls_back_to_the_shared_default() -> None:
    gone = PreferenceSetting.metric("revenue", "no_longer_exists", 1)
    effective = EffectivePreferences.build((), (gone,))
    assert effective.definition_preferences(CATALOG) == ()
