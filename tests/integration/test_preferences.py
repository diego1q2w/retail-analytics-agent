"""Preference memory against real PostgreSQL (needs Docker: ``pytest -m docker``)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import psycopg
import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import (
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.preferences import PreferenceAction, PreferenceService
from retail_analytics.bootstrap.access import build_access
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.domain.access import Permission, Role
from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.periods import OverrideScope
from retail_analytics.domain.preferences import (
    ADAPT_THRESHOLD,
    PROPOSAL_TTL,
    PreferenceKind,
    PreferenceSetting,
)
from tests.integration.compose_stack import Stack, running_stack
from tests.unit.preferences.fakes import Clock, RecordingInvalidator

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
SCOPES = frozenset(p.value for p in Permission)
TABLE = PreferenceSetting(PreferenceKind.TABLE_FORMAT, "table")
CURRENCY = PreferenceSetting(PreferenceKind.DISPLAY_CURRENCY, "EUR")
REVENUE = PreferenceSetting.metric("revenue", "completed_item_sales", 1)


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Env:
    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.clock = Clock()
        self.db: Persistence = build_persistence(stack.app_url, clock=self.clock)
        self.invalidator = RecordingInvalidator()
        self.service: PreferenceService = build_preferences(
            self.db,
            build_access(self.db, verifier=None),  # type: ignore[arg-type]
            invalidator=self.invalidator,
        )

    async def executive(self) -> tuple[Principal, str]:
        executive_id = _id("exec")
        await self.db.access_admin.register_executive(
            ExecutiveRegistration(
                executive_id=executive_id,
                issuer="iss",
                subject=f"sub-{executive_id}",
                roles=frozenset({Role.EXECUTIVE}),
                label="Test executive",
            )
        )
        session = await self.db.sessions.create_session(_id("ses"), executive_id)
        return Principal(executive_id, SCOPES), session.session_id


@pytest.fixture
def env(stack: Stack) -> Iterator[Env]:
    e = Env(stack)
    yield e
    e.db.close()


async def test_defaults_survive_a_new_session_and_service_instance(env: Env) -> None:
    alice, _ = await env.executive()
    out = await env.service.remember(alice, REVENUE)
    assert out.action is PreferenceAction.REMEMBERED
    await env.service.remember(alice, CURRENCY)
    second = await env.db.sessions.create_session(_id("ses"), alice.executive_id)

    fresh = build_preferences(env.db, build_access(env.db, verifier=None))  # type: ignore[arg-type]
    effective = await fresh.effective(alice, session_id=second.session_id)
    assert effective.value(PreferenceKind.DISPLAY_CURRENCY) == "EUR"
    assert effective.definition_preferences(default_catalog())[0].metric_id == (
        "completed_item_sales"
    )


async def test_two_executives_are_isolated_and_audit_never_stores_values(
    env: Env, stack: Stack
) -> None:
    alice, a_session = await env.executive()
    bob, _ = await env.executive()
    await env.service.remember(alice, CURRENCY)
    await env.service.remember(
        alice, PreferenceSetting(PreferenceKind.DISPLAY_CURRENCY, "GBP")
    )
    assert (await env.service.effective(bob)).value(
        PreferenceKind.DISPLAY_CURRENCY
    ) is None
    with pytest.raises(AccessDenied):
        await env.service.inspect(bob, session_id=a_session)
    await env.service.forget(alice, "display_currency")
    assert (await env.service.inspect(alice)).preferences == ()

    with psycopg.connect(stack.app_dsn) as conn:
        rows = conn.execute(
            "select action, version, source from preference_events "
            "where executive_id = %s order by id",
            (alice.executive_id,),
        ).fetchall()
        assert rows == [
            ("remembered", 1, "explicit"),
            ("changed", 2, "explicit"),
            ("forgotten", 2, None),
        ]
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute(
                "update preference_events set action = 'changed' "
                "where executive_id = %s",
                (alice.executive_id,),
            )


async def test_inference_needs_confirmation_and_is_atomic(env: Env) -> None:
    alice, session = await env.executive()
    for _ in range(ADAPT_THRESHOLD):
        out = await env.service.observe(alice, session, TABLE)
    assert out.action is PreferenceAction.PROPOSED
    other = await env.db.sessions.create_session(_id("ses"), alice.executive_id)
    assert (await env.service.effective(alice, session_id=session)).value(
        PreferenceKind.TABLE_FORMAT
    ) == "table"
    assert (await env.service.effective(alice, session_id=other.session_id)).value(
        PreferenceKind.TABLE_FORMAT
    ) is None

    bob, _ = await env.executive()
    with pytest.raises(AccessDenied):
        await env.service.confirm(bob, out.proposal_id or "")
    done = await env.service.confirm(alice, out.proposal_id or "")
    assert done.action is PreferenceAction.CONFIRMED
    assert (await env.service.effective(alice, session_id=other.session_id)).value(
        PreferenceKind.TABLE_FORMAT
    ) == "table"
    with pytest.raises(InvalidTransition):
        await env.service.confirm(alice, out.proposal_id or "")


async def test_expiry_and_decline_never_persist(env: Env) -> None:
    alice, session = await env.executive()
    for _ in range(ADAPT_THRESHOLD):
        out = await env.service.observe(alice, session, TABLE)
    env.clock.now += PROPOSAL_TTL + timedelta(seconds=1)
    assert (await env.service.inspect(alice)).proposals == ()
    with pytest.raises(InvalidTransition):
        await env.service.confirm(alice, out.proposal_id or "")

    carol, c_session = await env.executive()
    for _ in range(ADAPT_THRESHOLD):
        out = await env.service.observe(carol, c_session, TABLE)
    await env.service.decline(carol, out.proposal_id or "")
    again = await env.db.sessions.create_session(_id("ses"), carol.executive_id)
    for _ in range(ADAPT_THRESHOLD + 1):
        result = await env.service.observe(carol, again.session_id, TABLE)
    assert result.action is PreferenceAction.OBSERVED
    assert (await env.service.effective(carol)).value(
        PreferenceKind.TABLE_FORMAT
    ) is None


async def test_metric_change_invalidates_but_format_change_does_not(env: Env) -> None:
    alice, session = await env.executive()
    await env.service.remember(alice, TABLE)
    assert env.invalidator.calls == []
    await env.service.remember(
        alice, REVENUE, scope=OverrideScope.SESSION, session_id=session
    )
    assert env.invalidator.calls == [
        (alice.executive_id, session, "metric_definition:revenue")
    ]
    assert await env.service.forget_everything(alice) == 2
    assert (await env.service.effective(alice, session_id=session)).entries == ()
