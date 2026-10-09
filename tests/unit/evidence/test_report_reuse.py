"""Reusing the owner's saved-report evidence in another session (T18-F2).

The only gates are the automatic ones: owner, current products cover the
computed scope, intact, not invalidated, meaning still compatible. Imported
records are historical snapshots with their source, re-judged on every use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from retail_analytics.application.evidence import ReuseRequest, query_subject_key
from retail_analytics.application.result_privacy import PRIVACY_POLICY_VERSION
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.context import (
    EvidenceStanding,
    HistoryRules,
    HistoryTreatment,
    standings_by_id,
)
from retail_analytics.domain.conversation import Message, MessageRole
from retail_analytics.domain.evidence import (
    AnalysisCompatibility,
    Evidence,
    ReportSource,
    ReuseBlock,
    ReuseIntent,
)
from tests.unit.evidence.support import (
    EXEC_A,
    EXEC_B,
    FINGERPRINT,
    REVENUE,
    SCOPE_A,
    Env,
    basis,
    compiled_and_released,
    context,
    operation,
    requirements,
)

pytestmark = pytest.mark.asyncio

WIDER = ProductScope(SCOPE_A.product_ids | {"2"}, SCOPE_A.entitlement_version + 1)
NARROWER = ProductScope(frozenset({"1"}), SCOPE_A.entitlement_version + 1)


def compatibility(**overrides: Any) -> AnalysisCompatibility:
    compiled, _ = compiled_and_released()
    values: dict[str, Any] = {
        "catalog_version": compiled.catalog_version,
        "policy_version": PRIVACY_POLICY_VERSION,
        "preference_fingerprint": FINGERPRINT,
        "current_definitions": {REVENUE.metric_id: REVENUE.version},
    }
    values.update(overrides)
    return AnalysisCompatibility(**values)


async def _saved(env: Env) -> Evidence:
    """Evidence computed in session A (what a saved report cites)."""
    compiled, released = compiled_and_released()
    return await env.service.record_query(
        operation(context(), "op-1"), compiled, released, basis()
    )


async def _import(
    env: Env,
    evidence: Evidence,
    *,
    scope: ProductScope = SCOPE_A,
    executive: str = EXEC_A,
    compat: AnalysisCompatibility | None = None,
) -> Any:
    ctx = context(executive, scope, session="ses-new", run="run-new")
    return await env.service.import_report_evidence(
        ctx,
        report_id="rep-1",
        report_version=2,
        report_title="September spend",
        evidence_ids=[evidence.evidence_id],
        compatibility=compat or compatibility(),
    )


async def _standing(
    env: Env, evidence: Evidence, scope: ProductScope = SCOPE_A
) -> EvidenceStanding:
    session = await env.service.session_standing(
        context(scope=scope, session="ses-new", run="run-later"),
        run_ids=["run-new"],
    )
    return {s.evidence.evidence_id: s for s in session.standings}[evidence.evidence_id]


async def test_same_owner_new_session_reuses_with_source_and_date() -> None:
    env = Env()
    evidence = await _saved(env)

    outcome = await _import(env, evidence)

    assert outcome.linked == (evidence.evidence_id,)
    (line,) = dict(outcome.sources).values()
    assert 'from saved report "September spend" (v2)' in line
    assert "computed 2026-10-08 12:00 UTC" in line
    assert "period" in line and "completed_item_sales@1" in line
    assert "historical snapshot, not current data" in line
    standing = await _standing(env, evidence)
    assert standing.usable and standing.source is not None
    assert standing.source.report_id == "rep-1"
    links = await env.store.for_run("run-new")
    assert [x.evidence_id for x in links] == [evidence.evidence_id]
    # Usable for the session's other uses too (citations, derived results).
    later = context(session="ses-new", run="run-later")
    assert await env.service.usable_in_session(later) == (evidence,)
    assert await env.service.link_to_run(later, [evidence.evidence_id]) == (
        evidence.evidence_id,
    )


async def test_without_an_import_other_session_evidence_stays_withheld() -> None:
    env = Env()
    evidence = await _saved(env)
    ctx = context(session="ses-new", run="run-new")
    assert await env.service.usable_in_session(ctx) == ()
    assert await env.service.link_to_run(ctx, [evidence.evidence_id]) == ()


async def test_narrowed_scope_refuses_and_withholds_earlier_imports() -> None:
    env = Env()
    evidence = await _saved(env)

    refused = await _import(env, evidence, scope=NARROWER)
    assert refused.linked == ()
    assert refused.refused == (
        (evidence.evidence_id, ReuseBlock.AUTHORIZATION_CHANGED),
    )

    await _import(env, evidence)  # imported while access still covered it
    standing = await _standing(env, evidence, NARROWER)
    assert standing.block is ReuseBlock.AUTHORIZATION_CHANGED
    assert standing.access_withdrawn


async def test_widened_scope_still_reuses() -> None:
    env = Env()
    evidence = await _saved(env)
    outcome = await _import(env, evidence, scope=WIDER)
    assert outcome.linked == (evidence.evidence_id,)
    assert (await _standing(env, evidence, WIDER)).usable


async def test_other_executive_cannot_import() -> None:
    env = Env()
    evidence = await _saved(env)
    outcome = await _import(env, evidence, executive=EXEC_B)
    assert outcome.linked == ()
    assert outcome.refused == ((evidence.evidence_id, ReuseBlock.NOT_OWNED),)
    assert env.store.imports == {}


async def test_changed_definition_or_settings_are_not_reused() -> None:
    env = Env()
    evidence = await _saved(env)
    newer = compatibility(current_definitions={REVENUE.metric_id: 2})
    assert (await _import(env, evidence, compat=newer)).refused == (
        (evidence.evidence_id, ReuseBlock.DEFINITIONS_CHANGED),
    )
    other_settings = compatibility(preference_fingerprint="fp-other")
    assert (await _import(env, evidence, compat=other_settings)).refused == (
        (evidence.evidence_id, ReuseBlock.PREFERENCES_CHANGED),
    )
    await env.store.invalidate_dependent_findings(
        EXEC_A, None, "metric_definition:revenue"
    )
    assert (await _import(env, evidence)).refused == (
        (evidence.evidence_id, ReuseBlock.INVALIDATED),
    )
    assert env.store.imports == {}


async def test_unrecorded_definitions_are_unknown_and_never_reused() -> None:
    """A record that did not record its definitions (made before T18-F3) is
    "compatibility unknown", never "compatible": the agent recomputes."""
    env = Env()
    compiled, released = compiled_and_released()
    legacy = await env.service.record_query(
        operation(context(), "op-legacy"),
        compiled,
        released,
        basis(
            definitions=frozenset(),
            terms=frozenset(),
            date_basis=None,
            definitions_recorded=False,
        ),
    )
    assert not legacy.content.analysis.definitions_recorded

    outcome = await _import(env, legacy)

    assert outcome.linked == ()
    assert outcome.refused == ((legacy.evidence_id, ReuseBlock.DEFINITIONS_UNKNOWN),)
    assert env.store.imports == {}


async def test_session_setting_change_supersedes_an_import() -> None:
    env = Env()
    evidence = await _saved(env)
    await _import(env, evidence)
    await env.store.invalidate_dependent_findings(
        EXEC_A, "ses-new", "metric_definition:revenue"
    )
    assert (await _standing(env, evidence)).block is ReuseBlock.INVALIDATED
    # The record itself stays valid for its own session.
    assert evidence.evidence_id not in env.store.invalidated


async def test_current_questions_requery_while_explanations_reuse() -> None:
    env = Env()
    evidence = await _saved(env)
    await _import(env, evidence)
    env.clock.advance(timedelta(days=2))
    compiled, _ = compiled_and_released()
    ctx = context(session="ses-new", run="run-later")

    def request(intent: ReuseIntent) -> ReuseRequest:
        return ReuseRequest(
            intent, requirements(compiled), subject_key=query_subject_key(compiled)
        )

    current = await env.service.find_reusable(ctx, request(ReuseIntent.CURRENT))
    assert current.reused is None
    assert current.considered == ((evidence.evidence_id, ReuseBlock.STALE),)
    # A report snapshot from another session is never refreshed in place.
    assert current.refresh_of is None
    explain = await env.service.find_reusable(ctx, request(ReuseIntent.EXPLAIN))
    assert explain.reused is not None
    assert explain.reused.evidence.evidence_id == evidence.evidence_id


async def test_history_judges_imports_by_when_they_entered_the_session() -> None:
    """An old snapshot linked in today neither cleans nor taints older text."""
    computed = datetime(2026, 9, 1, tzinfo=UTC)
    imported_at = datetime(2026, 10, 8, 12, tzinfo=UTC)

    def evidence_like(evidence_id: str) -> Any:
        class _E:
            pass

        e: Any = _E()
        e.evidence_id = evidence_id
        e.computed_at = computed
        return e

    source = ReportSource("rep-1", 1, "t", imported_at)
    withheld = EvidenceStanding(
        evidence_like("evd_old"), ReuseBlock.AUTHORIZATION_CHANGED, source
    )
    rules = HistoryRules(standings_by_id([withheld]), {"run-x": frozenset()})
    before = Message(
        "m1", "ses-new", MessageRole.ASSISTANT, "x", imported_at - timedelta(hours=1)
    )
    before = _with_run(before, "run-x")
    after = _with_run(
        Message(
            "m2",
            "ses-new",
            MessageRole.ASSISTANT,
            "x",
            imported_at + timedelta(hours=1),
        ),
        "run-x",
    )
    assert rules.treatment(before) is HistoryTreatment.INCLUDE
    assert rules.treatment(after) is HistoryTreatment.WITHHELD_ACCESS_CHANGED

    usable = EvidenceStanding(evidence_like("evd_ok"), None, source)
    assert HistoryRules(standings_by_id([usable]), {}).current_since is None


def _with_run(message: Message, run_id: str) -> Message:
    from dataclasses import replace

    return replace(message, run_id=run_id)
