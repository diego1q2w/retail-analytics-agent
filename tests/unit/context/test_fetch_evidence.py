"""fetch_evidence: compacted or omitted evidence stays reachable, and only that."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.tools import (
    ExecutionContext,
    OperationContext,
)
from retail_analytics.application.tools import (
    CapabilityRegistry,
    ToolFailed,
    ToolSucceeded,
)
from retail_analytics.capabilities.evidence import (
    FETCH_EVIDENCE,
    FetchEvidenceInput,
    fetch_evidence_capability,
)
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.context import ContextBudget
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.context.support import BRAND_SQL, SPEND_SQL, A, B, World
from tests.unit.privacy.support import EXEC_A, EXEC_B, PII_STRINGS

pytestmark = pytest.mark.asyncio


async def test_compacted_evidence_can_be_read_in_pages() -> None:
    w = World(budget=ContextBudget(max_tokens=500, max_rows_per_evidence=1))
    r1 = w.new_run()
    evidence, released = await w.query(r1)
    total = len(released.records())
    assert total >= 2
    ctx = await w.builder.build(A, w.new_run(), "Again")
    assert ctx.evidence[0].evidence_id == evidence.evidence_id
    assert len(ctx.evidence[0].rows) <= 1 and "fetch_evidence" in ctx.render()

    r2 = w.new_run()
    first = await w.builder.read_evidence(A, r2, evidence.evidence_id, limit=1)
    assert first is not None
    assert (first.offset, len(first.rows), first.total_rows) == (0, 1, total)
    assert first.next_offset == 1
    second = await w.builder.read_evidence(
        A, r2, evidence.evidence_id, offset=first.next_offset
    )
    assert second is not None and second.offset == 1
    assert len(first.rows) + len(second.rows) == total and second.next_offset is None


async def test_listing_shows_usable_evidence_without_rows() -> None:
    w = World()
    r1 = w.new_run()
    evidence, _ = await w.query(r1)
    listing = await w.builder.list_evidence(A, w.new_run())
    assert [x.evidence_id for x in listing] == [evidence.evidence_id]
    assert not hasattr(listing[0], "rows")


async def test_page_is_bounded_by_the_context_budget() -> None:
    w = World(budget=ContextBudget(max_tokens=500))
    r1 = w.new_run()
    rows = tuple((f"label {i} " + "x" * 80, i) for i in range(50))
    evidence = await w.external(r1, rows)
    page = await w.builder.read_evidence(A, r1, evidence.evidence_id)
    assert page is not None
    assert 0 < len(page.rows) < 50
    assert sum(len(c) for r in page.rows for c in r) <= 500 * 4 // 2 + 200
    assert page.next_offset == len(page.rows)


async def test_truncation_flag_is_preserved() -> None:
    w = World()
    r1 = w.new_run()
    evidence = await w.external(r1, (("a", 1), ("b", 2)), truncation="row_limit")
    page = await w.builder.read_evidence(A, r1, evidence.evidence_id)
    assert page is not None and page.truncated_at_source
    (listed,) = await w.builder.list_evidence(A, r1)
    assert listed.truncated_at_source


async def test_authority_is_resolved_on_every_call() -> None:
    w = World()
    r1 = w.new_run()
    wide, _ = await w.query(r1, BRAND_SQL)
    assert await w.builder.read_evidence(A, r1, wide.evidence_id) is not None
    w.set_products(EXEC_A, frozenset({"1", "3"}))
    assert await w.builder.read_evidence(A, r1, wide.evidence_id) is None
    assert await w.builder.list_evidence(A, r1) == ()
    w.set_products(EXEC_A, frozenset())
    assert await w.builder.list_evidence(A, r1) == ()


async def test_deactivated_executive_is_denied() -> None:
    w = World()
    r1 = w.new_run()
    evidence, _ = await w.query(r1)
    current = w.directory.by_id[EXEC_A]
    w.directory.by_id[EXEC_A] = current.__class__(
        executive_id=EXEC_A,
        roles=current.roles,
        product_ids=current.product_ids,
        active=False,
        authorization_version=current.authorization_version + 1,
    )
    with pytest.raises(AccessDenied):
        await w.builder.read_evidence(A, r1, evidence.evidence_id)
    with pytest.raises(AccessDenied):
        await w.builder.list_evidence(A, r1)


async def test_other_executives_and_sessions_are_unreachable() -> None:
    w = World()
    rb = w.new_run(EXEC_B, "s-b")
    theirs, _ = await w.query(rb, principal=B)
    ra = w.new_run()
    assert await w.builder.read_evidence(A, ra, theirs.evidence_id) is None
    assert await w.builder.list_evidence(A, ra) == ()
    with pytest.raises(AccessDenied):
        await w.builder.read_evidence(A, rb, theirs.evidence_id)
    other_session = w.new_run(EXEC_A, "s-a")
    w.records.add(EXEC_A, "a2")
    assert await w.builder.read_evidence(A, other_session, "evd_unknown") is None


async def test_topic_reset_hides_earlier_evidence() -> None:
    w = World()
    r1 = w.new_run()
    evidence, _ = await w.query(r1)
    w.tick()
    await w.builder.reset_topic(A, "s-a", "reset-1")
    w.tick()
    assert await w.builder.read_evidence(A, w.new_run(), evidence.evidence_id) is None


async def test_invalidated_evidence_is_not_readable() -> None:
    w = World()
    r1 = w.new_run()
    evidence, _ = await w.query(r1)
    w.store.tamper(evidence.evidence_id, subject_key="q:other")
    assert await w.builder.read_evidence(A, r1, evidence.evidence_id) is None


async def test_pages_never_carry_personal_data() -> None:
    w = World()
    r1 = w.new_run()
    evidence, _ = await w.query(r1, SPEND_SQL)
    page = await w.builder.read_evidence(A, r1, evidence.evidence_id)
    assert page is not None
    text = repr(page)
    for needle in PII_STRINGS:
        assert needle not in text
    masked = await w.external(
        r1, (("write to jane.doe@example.com", 1),), columns=("note", "amount")
    )
    page = await w.builder.read_evidence(A, r1, masked.evidence_id)
    assert page is not None and page.masked
    assert "jane.doe@example.com" not in repr(page)


@dataclass
class _Principals:
    principal: Principal | None = A

    async def record(self, run_id: str, principal: Principal) -> Principal:
        return principal

    async def get(self, run_id: str) -> Principal | None:
        return self.principal


def _op(run_id: str, *, executive: str = EXEC_A) -> OperationContext:
    return OperationContext(
        ExecutionContext(
            executive_id=executive,
            permissions=frozenset(p.value for p in Permission),
            product_scope=ProductScope(frozenset({"1"}), 1),
            correlation=Correlation(session_id="s-a", run_id=run_id),
        ),
        "op-1",
        1,
    )


async def test_the_tool_lists_reads_and_refuses_uniformly() -> None:
    w = World()
    r1 = w.new_run()
    evidence, _ = await w.query(r1)
    spec = fetch_evidence_capability(w.builder, principals=_Principals())
    CapabilityRegistry((spec,))  # passes the registry safety rules
    ctx = _op(r1)

    listed = await spec.handler(FetchEvidenceInput(), ctx)
    assert isinstance(listed, ToolSucceeded) and listed.output.listing
    assert listed.output.listing[0].evidence_id == evidence.evidence_id

    read = await spec.handler(FetchEvidenceInput(evidence_id=evidence.evidence_id), ctx)
    assert isinstance(read, ToolSucceeded) and read.output.evidence is not None
    assert read.output.evidence.rows

    missing = await spec.handler(FetchEvidenceInput(evidence_id="evd_nope"), ctx)
    assert isinstance(missing, ToolFailed)
    assert missing.code is ToolErrorCode.FIELD_UNAVAILABLE

    nobody = fetch_evidence_capability(w.builder, principals=_Principals(None))
    denied = await nobody.handler(FetchEvidenceInput(), ctx)
    assert isinstance(denied, ToolFailed) and denied.code is ToolErrorCode.ACCESS_DENIED
    assert spec.name == FETCH_EVIDENCE
