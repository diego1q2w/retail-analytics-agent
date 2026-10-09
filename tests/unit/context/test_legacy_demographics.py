"""Evidence recorded before demographics became aggregate-only (T09-F1).

A legacy record that holds or was selected by individual-level demographics
must not reach model context, ``fetch_evidence``, chat history, citations or
released text; a verifiably group-level legacy record keeps working; anything
unverifiable fails closed. Stored records are never rewritten.
"""

from __future__ import annotations

import pytest

from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.evidence import EvidenceRejected, EvidenceService
from retail_analytics.application.output_privacy import (
    ACCESS_CHANGED,
    OutputDestination,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.evidence import (
    EvidenceColumn,
    EvidenceKind,
    ReusePolicy,
)
from tests.unit.context.support import A, World
from tests.unit.evidence.fakes import Ids
from tests.unit.privacy.support import EXEC_A, ref

pytestmark = pytest.mark.asyncio

USER, ASSISTANT = MessageRole.USER, MessageRole.ASSISTANT
REF = ref(EXEC_A, "customer_ref", 10)

PROFILE_SQL = (
    "SELECT `customers`.`customer_ref` AS `customer_ref`, `customers`.`state` AS "
    "`state`, `customers`.`age_band` AS `age_band` FROM `customers` AS `customers`"
)
PROFILE_COLUMNS = (
    EvidenceColumn("customer_ref", "reference", ("customers.customer_ref",)),
    EvidenceColumn("state", "value", ("customers.state",)),
    EvidenceColumn("age_band", "age_band", ("customers.age_band",)),
)
# No reference column: the profile hides in the predicate.
TARGETED_SQL = (
    "SELECT `c`.`state` AS `state`, `c`.`age_band` AS `age_band` FROM "
    "`customers` AS `c` WHERE `c`.`customer_ref` = @_value_0"
)
STATE_COLUMNS = (
    EvidenceColumn("state", "value", ("customers.state",)),
    EvidenceColumn("age_band", "age_band", ("customers.age_band",)),
)
AGGREGATE_SQL = (
    "SELECT `c`.`state` AS `state`, COUNT(DISTINCT `c`.`customer_ref`) AS `n` "
    "FROM `customers` AS `c` GROUP BY 1"
)
AGGREGATE_COLUMNS = (
    EvidenceColumn("state", "value", ("customers.state",)),
    EvidenceColumn("n", "value", ("customers.customer_ref",)),
)


async def _profile(w: World, run_id: str) -> str:
    record = await w.legacy(
        run_id,
        sql=PROFILE_SQL,
        columns=PROFILE_COLUMNS,
        rows=((REF, "CA", "25-29"),),
    )
    return record.evidence_id


async def test_legacy_profile_never_reaches_context_fetch_or_history() -> None:
    w = World()
    r1 = w.new_run()
    w.say(USER, "Profile of my top customer", r1)
    evidence_id = await _profile(w, r1)
    w.tick()
    w.say(ASSISTANT, f"Customer {REF} lives in CA and is 25-29 [{evidence_id}]", r1)

    r2 = w.new_run()
    ctx = await w.builder.build(A, r2, "Tell me more")
    assert ctx.evidence == ()
    assert ctx.omissions.evidence_withheld == 1
    assert ctx.omissions.history_access_changed == 1
    rendered = ctx.render()
    assert REF not in rendered and "25-29" not in rendered
    assert await w.builder.read_evidence(A, r2, evidence_id) is None
    # Stored as it was: withheld, never rewritten or purged.
    stored = await w.store.get(evidence_id)
    assert stored is not None and stored.evidence.is_intact


async def test_answers_and_citations_resting_on_legacy_profiles_are_withheld() -> None:
    w = World()
    r1 = w.new_run()
    evidence_id = await _profile(w, r1)
    policy = await w.gate.policy_for_run(A, r1)
    assert evidence_id not in policy.usable_evidence
    assert policy.run_evidence_withdrawn
    with pytest.raises(OutputWithheld) as caught:
        await w.gate.check(
            A,
            r1,
            [OutputSection("answer", "Based on the result.", (evidence_id,))],
            OutputDestination.DISPLAY,
        )
    assert caught.value.reason == "unavailable_evidence"
    with pytest.raises(OutputWithheld) as uncited:
        await w.gate.check(
            A, r1, [OutputSection("answer", "Done.")], OutputDestination.DISPLAY
        )
    assert uncited.value.reason == ACCESS_CHANGED


async def test_profile_hidden_in_a_predicate_is_withheld() -> None:
    w = World()
    r1 = w.new_run()
    record = await w.legacy(
        r1, sql=TARGETED_SQL, columns=STATE_COLUMNS, rows=(("CA", "25-29"),)
    )
    assert await w.evidence.privacy_withdrawn(record)
    ctx = await w.builder.build(A, w.new_run(), "Again")
    assert ctx.evidence == ()


async def test_verifiably_group_level_legacy_evidence_still_works() -> None:
    w = World()
    r1 = w.new_run()
    grouped = await w.legacy(
        r1, sql=AGGREGATE_SQL, columns=AGGREGATE_COLUMNS, rows=(("CA", 2),)
    )
    products = await w.legacy(
        r1,
        sql="SELECT `p`.`brand` AS `brand` FROM `products` AS `p`",
        columns=(EvidenceColumn("brand", "value", ("products.brand",)),),
        rows=(("Alpha",),),
        relations=("products",),
    )
    ctx = await w.builder.build(A, w.new_run(), "Again")
    assert {e.evidence_id for e in ctx.evidence} == {
        grouped.evidence_id,
        products.evidence_id,
    }
    page = await w.builder.read_evidence(A, w.new_run(), grouped.evidence_id)
    assert page is not None


async def test_unverifiable_legacy_demographics_fail_closed() -> None:
    w = World()
    r1 = w.new_run()
    grouped = await w.legacy(
        r1, sql=AGGREGATE_SQL, columns=AGGREGATE_COLUMNS, rows=(("CA", 2),)
    )
    # Without a query audit nothing establishes that the record is safe.
    blind = EvidenceService(
        w.store, w.store, clock=w.clock, new_id=Ids(), policy=ReusePolicy()
    )
    assert await blind.privacy_withdrawn(grouped)
    # Nor a record with a reference-shaped cell, whatever its query says.
    leaked = await w.legacy(
        r1, sql=AGGREGATE_SQL, columns=AGGREGATE_COLUMNS, rows=((REF, 2),)
    )
    assert await w.evidence.privacy_withdrawn(leaked)


async def test_derived_evidence_follows_its_legacy_inputs() -> None:
    w = World()
    r1 = w.new_run()
    profile_id = await _profile(w, r1)
    grouped = await w.legacy(
        r1, sql=AGGREGATE_SQL, columns=AGGREGATE_COLUMNS, rows=(("CA", 2),)
    )
    from_profile = await w.legacy(
        r1,
        sql=None,
        columns=PROFILE_COLUMNS,
        rows=((REF, "CA", "25-29"),),
        kind=EvidenceKind.DERIVED,
        derived_from=(profile_id,),
    )
    from_grouped = await w.legacy(
        r1,
        sql=None,
        columns=AGGREGATE_COLUMNS,
        rows=(("CA", 2),),
        kind=EvidenceKind.DERIVED,
        derived_from=(grouped.evidence_id,),
    )
    assert await w.evidence.privacy_withdrawn(from_profile)
    assert not await w.evidence.privacy_withdrawn(from_grouped)
    # New calculations cannot start from a withheld legacy record either.
    ctx = await w.resolver.context_for_run(A, r1)
    content = from_profile.content
    with pytest.raises(EvidenceRejected) as caught:
        await w.evidence.record(OperationContext(ctx, "op-new"), content)
    assert caught.value.reason == "input_unavailable"
