"""EvidenceService: recording from released results and guarded reuse."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.persistence import IdempotencyConflict
from retail_analytics.application.contracts.query_compiler import CompiledQuery
from retail_analytics.application.evidence import (
    EvidenceRejected,
    ReuseRequest,
    query_subject_key,
)
from retail_analytics.application.result_privacy import ReleasedResult
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    DefinitionRef,
    Evidence,
    EvidenceColumn,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    EvidenceUse,
    PinHolder,
    Provenance,
    ReuseBlock,
    ReuseIntent,
    encode_provenance,
)
from tests.unit.evidence.support import (
    EXEC_A,
    EXEC_B,
    FINGERPRINT,
    REVENUE,
    SCOPE_A,
    SCOPE_B,
    SEPTEMBER,
    Env,
    basis,
    compiled_and_released,
    context,
    operation,
    requirements,
)
from tests.unit.privacy.support import MASTER_KEY

pytestmark = pytest.mark.asyncio


async def _recorded(env: Env) -> tuple[CompiledQuery, ReleasedResult, Evidence]:
    compiled, released = compiled_and_released()
    ctx = context()
    evidence = await env.service.record_query(
        operation(ctx, "op-1"), compiled, released, basis()
    )
    return compiled, released, evidence


def _request(
    compiled: CompiledQuery, intent: ReuseIntent = ReuseIntent.CURRENT, **req: Any
) -> ReuseRequest:
    return ReuseRequest(
        intent, requirements(compiled, **req), subject_key=query_subject_key(compiled)
    )


async def test_query_evidence_keeps_analysis_parameters_only() -> None:
    env = Env()
    compiled, released, evidence = await _recorded(env)
    secrets = [p for p in compiled.parameters if p.secret]
    assert secrets, "fixture must exercise secret reference parameters"

    provenance = evidence.content.provenance
    assert [p.name for p in provenance.parameters] == ["min_amount"]
    assert provenance.logical_sql == compiled.logical_sql
    assert provenance.executed_query_digest is not None
    stored_text = repr(encode_provenance(provenance)) + repr(evidence.content.table)
    assert "_policy_" not in stored_text
    assert compiled.sql not in stored_text
    for p in secrets:
        assert str(p.value) not in stored_text
    assert MASTER_KEY.decode() not in stored_text

    assert evidence.content.table.rows == released.rows
    assert evidence.content.table.columns[0].role == "reference"
    assert evidence.authority.authorization_version == SCOPE_A.entitlement_version
    assert evidence.content.analysis.policy_version == released.policy_version
    assert evidence.is_intact
    assert (evidence.lineage_id, evidence.version) == (evidence.evidence_id, 1)
    assert [(x.run_id, x.use) for x in await env.store.for_run("run-1")] == [
        ("run-1", EvidenceUse.PRODUCED)
    ]


async def test_retried_operation_returns_the_same_record() -> None:
    env = Env()
    compiled, released, first = await _recorded(env)
    again = await env.service.record_query(
        operation(context(), "op-1"), compiled, released, basis()
    )
    assert again == first
    assert len(env.store.records) == 1
    with pytest.raises(IdempotencyConflict):
        await env.service.record_query(
            operation(context(), "op-1"),
            compiled,
            released,
            basis(preference_fingerprint="changed"),
        )


async def test_follow_up_run_reuses_within_the_session() -> None:
    env = Env()
    compiled, _, evidence = await _recorded(env)
    env.clock.advance(timedelta(minutes=10))
    outcome = await env.service.find_reusable(context(run="run-2"), _request(compiled))
    assert outcome.reused is not None
    assert outcome.reused.evidence == evidence
    assert outcome.reused.snapshot.age == timedelta(minutes=10)
    assert [(x.evidence_id, x.use) for x in await env.store.for_run("run-2")] == [
        (evidence.evidence_id, EvidenceUse.REUSED)
    ]


async def test_current_data_older_than_fifteen_minutes_is_requeried() -> None:
    env = Env()
    compiled, _, old = await _recorded(env)
    env.clock.advance(timedelta(minutes=15, seconds=1))

    current = await env.service.find_reusable(context(run="run-2"), _request(compiled))
    assert current.reused is None
    assert current.considered == ((old.evidence_id, ReuseBlock.STALE),)
    assert current.refresh_of == old.evidence_id

    explained = await env.service.find_reusable(
        context(run="run-2"),
        ReuseRequest(
            ReuseIntent.EXPLAIN, requirements(compiled), evidence_id=old.evidence_id
        ),
    )
    assert explained.reused is not None
    assert explained.reused.snapshot.computed_at == old.computed_at
    assert "Snapshot computed at 2026-10-08 12:00 UTC" in (
        explained.reused.snapshot.describe()
    )


async def test_refresh_creates_a_new_version_and_keeps_the_snapshot() -> None:
    env = Env()
    compiled, _, old = await _recorded(env)
    refresh = await env.service.find_reusable(
        context(run="run-2"), _request(compiled, ReuseIntent.REFRESH)
    )
    assert refresh.reused is None
    assert refresh.considered == ((old.evidence_id, ReuseBlock.REFRESH_REQUESTED),)

    env.clock.advance(timedelta(minutes=1))
    _, newer_rows = compiled_and_released(values={"min_amount": 1})
    new = await env.service.record_query(
        operation(context(run="run-2"), "op-2"),
        compiled,
        newer_rows,
        basis(),
        refreshes=refresh.refresh_of,
    )
    assert (new.lineage_id, new.version) == (old.lineage_id, 2)
    assert new.evidence_id != old.evidence_id
    stored_old = await env.store.get(old.evidence_id)
    assert stored_old is not None and stored_old.evidence == old
    assert stored_old.evidence.is_intact

    latest = await env.service.find_reusable(context(run="run-3"), _request(compiled))
    assert latest.reused is not None and latest.reused.evidence == new


async def test_another_executive_cannot_see_or_refresh_evidence() -> None:
    env = Env()
    compiled, _, evidence = await _recorded(env)
    other = context(EXEC_B, SCOPE_B, session="ses-b", run="run-b")
    by_id = await env.service.find_reusable(
        other,
        ReuseRequest(
            ReuseIntent.EXPLAIN,
            requirements(compiled),
            evidence_id=evidence.evidence_id,
        ),
    )
    assert by_id.reused is None and by_id.considered == () and by_id.refresh_of is None
    by_subject = await env.service.find_reusable(other, _request(compiled))
    assert by_subject.reused is None and by_subject.considered == ()
    assert await env.service.usable_in_session(other) == ()

    b_compiled, b_released = compiled_and_released(EXEC_B, SCOPE_B)
    with pytest.raises(AccessDenied):
        await env.service.record_query(
            operation(other, "op-b"),
            b_compiled,
            b_released,
            basis(),
            refreshes=evidence.evidence_id,
        )
    # Same executive, another session: no reuse either.
    elsewhere = await env.service.find_reusable(
        context(session="ses-a2", run="run-x"), _request(compiled)
    )
    assert elsewhere.reused is None and elsewhere.considered == ()


async def test_entitlement_change_blocks_reuse_and_context() -> None:
    env = Env()
    compiled, _, evidence = await _recorded(env)
    changed = ProductScope(SCOPE_A.product_ids, SCOPE_A.entitlement_version + 1)
    outcome = await env.service.find_reusable(
        context(scope=changed, run="run-2"), _request(compiled, ReuseIntent.EXPLAIN)
    )
    assert outcome.reused is None
    assert outcome.considered == (
        (evidence.evidence_id, ReuseBlock.AUTHORIZATION_CHANGED),
    )
    assert await env.service.usable_in_session(context(scope=changed)) == ()
    assert await env.service.usable_in_session(context()) == (evidence,)


async def test_empty_scope_never_reads_reuses_or_records() -> None:
    env = Env()
    compiled, released, _ = await _recorded(env)
    empty = ProductScope(frozenset(), SCOPE_A.entitlement_version)
    outcome = await env.service.find_reusable(
        context(scope=empty, run="run-2"), _request(compiled)
    )
    assert outcome.reused is None and outcome.considered == ()
    assert await env.service.usable_in_session(context(scope=empty)) == ()
    with pytest.raises(EvidenceRejected) as caught:
        await env.service.record_query(
            operation(context(scope=empty), "op-2"), compiled, released, basis()
        )
    assert caught.value.reason == "no_product_scope"


async def test_results_released_under_old_authority_are_not_recorded() -> None:
    env = Env()
    compiled, released = compiled_and_released()
    newer = ProductScope(SCOPE_A.product_ids, SCOPE_A.entitlement_version + 1)
    with pytest.raises(EvidenceRejected) as caught:
        await env.service.record_query(
            operation(context(scope=newer), "op-1"), compiled, released, basis()
        )
    assert caught.value.reason == "stale_authorization"


async def test_definition_and_preference_changes_block_reuse() -> None:
    env = Env()
    compiled, _, evidence = await _recorded(env)
    v2 = frozenset({DefinitionRef(REVENUE.metric_id, 2)})
    for changes, block in (
        ({"definitions": v2}, ReuseBlock.DEFINITIONS_CHANGED),
        ({"preference_fingerprint": "fp-new"}, ReuseBlock.PREFERENCES_CHANGED),
        ({"catalog_version": 99}, ReuseBlock.CATALOG_CHANGED),
    ):
        outcome = await env.service.find_reusable(
            context(run="run-2"), _request(compiled, **changes)
        )
        assert outcome.reused is None
        assert outcome.considered == ((evidence.evidence_id, block),)


async def test_insufficient_or_truncated_granularity_blocks_reuse() -> None:
    env = Env()
    compiled, released = compiled_and_released(max_rows=2)
    assert released.truncated
    evidence = await env.service.record_query(
        operation(context(), "op-1"), compiled, released, basis()
    )
    assert evidence.content.table.truncated
    by_state = await env.service.find_reusable(
        context(run="run-2"), _request(compiled, grain=frozenset({"state"}))
    )
    assert by_state.considered == (
        (evidence.evidence_id, ReuseBlock.INSUFFICIENT_GRANULARITY),
    )
    as_input = await env.service.find_reusable(
        context(run="run-2"), _request(compiled, requires_complete=True)
    )
    assert as_input.considered == ((evidence.evidence_id, ReuseBlock.TRUNCATED),)
    shown = await env.service.find_reusable(context(run="run-2"), _request(compiled))
    assert shown.reused is not None


async def test_preference_change_invalidates_dependents_transitively() -> None:
    env = Env()
    compiled, _, evidence = await _recorded(env)
    derived = await env.service.record(
        operation(context(), "op-d"),
        EvidenceContent(
            kind=EvidenceKind.DERIVED,
            subject_key="share-of-spend",
            analysis=AnalysisStamp(
                compiled.catalog_version,
                1,
                frozenset({REVENUE}),
                FINGERPRINT,
                SEPTEMBER,
            ),
            provenance=Provenance(notes=(("method", "share of total"),)),
            table=EvidenceTable((EvidenceColumn("share", "value"),), ((0.5,),), 1),
            grain=(),
            derived_from=(evidence.evidence_id,),
        ),
    )
    await env.store.invalidate_dependent_findings(EXEC_A, None, "table_format")
    assert env.store.invalidated == set()
    await env.store.invalidate_dependent_findings(
        EXEC_A, "ses-a", "metric_definition:revenue"
    )
    assert env.store.invalidated == {evidence.evidence_id, derived.evidence_id}
    outcome = await env.service.find_reusable(context(run="run-2"), _request(compiled))
    assert outcome.considered == ((evidence.evidence_id, ReuseBlock.INVALIDATED),)
    assert await env.service.usable_in_session(context()) == ()


async def test_derived_evidence_needs_current_owned_inputs() -> None:
    env = Env()
    compiled, _, evidence = await _recorded(env)
    content = EvidenceContent(
        kind=EvidenceKind.DERIVED,
        subject_key="derived",
        analysis=AnalysisStamp(compiled.catalog_version, 1, frozenset(), FINGERPRINT),
        provenance=Provenance(),
        table=EvidenceTable((EvidenceColumn("x", "value"),), ((1,),), 1),
        grain=(),
        derived_from=(evidence.evidence_id,),
    )
    other = context(EXEC_B, SCOPE_B, session="ses-b", run="run-b")
    with pytest.raises(EvidenceRejected) as caught:
        await env.service.record(operation(other, "op-b"), content)
    assert caught.value.reason == "input_unavailable"
    changed = ProductScope(SCOPE_A.product_ids, SCOPE_A.entitlement_version + 1)
    with pytest.raises(EvidenceRejected):
        await env.service.record(operation(context(scope=changed), "op-c"), content)
    with pytest.raises(EvidenceRejected):
        await env.service.record(
            operation(context(), "op-q"),
            replace(
                content,
                kind=EvidenceKind.QUERY,
                provenance=Provenance(
                    logical_sql="SELECT 1", executed_query_digest="d"
                ),
            ),
        )


async def test_pins_are_owner_only() -> None:
    env = Env()
    _, _, evidence = await _recorded(env)
    report = PinHolder("report", "rep-1")
    with pytest.raises(AccessDenied):
        await env.service.pin_for(EXEC_B, [evidence.evidence_id], report)
    await env.service.pin_for(
        EXEC_A, [evidence.evidence_id, evidence.evidence_id], report
    )
    assert await env.store.pinned(report) == (evidence.evidence_id,)
    assert await env.store.holders(evidence.evidence_id) == (report,)
    assert await env.service.release_pins(report) == 1
    assert await env.store.pinned(report) == ()


async def test_analysis_permission_is_required() -> None:
    env = Env()
    compiled, released = compiled_and_released()
    no_perm = context(permissions=frozenset())
    with pytest.raises(AccessDenied):
        await env.service.record_query(
            operation(no_perm, "op-1"), compiled, released, basis()
        )
    with pytest.raises(AccessDenied):
        await env.service.find_reusable(no_perm, _request(compiled))


async def test_tampered_record_is_not_reused() -> None:
    env = Env()
    compiled, _, evidence = await _recorded(env)
    env.store.tamper(evidence.evidence_id, grain=())
    outcome = await env.service.find_reusable(context(run="run-2"), _request(compiled))
    assert outcome.considered == ((evidence.evidence_id, ReuseBlock.TAMPERED),)


async def test_subject_key_depends_on_query_and_values() -> None:
    a, _ = compiled_and_released(values={"min_amount": 1})
    b, _ = compiled_and_released(values={"min_amount": 2})
    again, _ = compiled_and_released(values={"min_amount": 1})
    assert query_subject_key(a) == query_subject_key(again)
    assert query_subject_key(a) != query_subject_key(b)
    # Keys are per logical question, not per executive's secret reference key.
    other, _ = compiled_and_released(EXEC_B, SCOPE_B)
    assert query_subject_key(other) == query_subject_key(a)


def _service_from_settings(env: Env, seconds: int | None) -> Any:
    from types import SimpleNamespace

    from retail_analytics.bootstrap.config import load_backend_settings
    from retail_analytics.bootstrap.evidence import build_evidence

    raw = (
        {}
        if seconds is None
        else {"RETAIL_ANALYTICS_EVIDENCE_CURRENT_FRESHNESS_SECONDS": str(seconds)}
    )
    return build_evidence(
        SimpleNamespace(evidence=env.store, engine=None),  # type: ignore[arg-type]
        settings=load_backend_settings(environ=raw, env_file=None),
        clock=env.clock,
    )


@pytest.mark.parametrize(
    ("seconds", "age", "reused"),
    [
        (None, timedelta(minutes=14), True),
        (None, timedelta(minutes=16), False),
        (120, timedelta(minutes=3), False),
        (3600, timedelta(minutes=30), True),
    ],
)
async def test_configured_freshness_limit_changes_reuse(
    seconds: int | None, age: timedelta, reused: bool
) -> None:
    env = Env()
    compiled, _, _ = await _recorded(env)
    service = _service_from_settings(env, seconds)
    env.clock.advance(age)
    outcome = await service.find_reusable(context(run="run-2"), _request(compiled))
    assert (outcome.reused is not None) is reused


async def test_explicit_constructor_value_overrides_settings() -> None:
    from types import SimpleNamespace

    from retail_analytics.bootstrap.evidence import build_evidence

    env = Env()
    compiled, _, _ = await _recorded(env)
    service = build_evidence(
        SimpleNamespace(evidence=env.store, engine=None),  # type: ignore[arg-type]
        current_freshness=timedelta(minutes=1),
        clock=env.clock,
    )
    env.clock.advance(timedelta(minutes=2))
    outcome = await service.find_reusable(context(run="run-2"), _request(compiled))
    assert outcome.reused is None
