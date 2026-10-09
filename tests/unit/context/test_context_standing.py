"""Prompt selection is bounded; the authoritative standing behind it is not.

``ModelContext.current_evidence``/``current_history`` list what is valid now
(including what count, size and scan bounds left out of the prompt), so a
conversation can be judged by validity rather than by what one request shows.
"""

from __future__ import annotations

import pytest

from retail_analytics.domain.context import ContextBudget
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.evidence import Evidence
from tests.unit.context.support import NARROW, A, World
from tests.unit.privacy.support import EXEC_A

pytestmark = pytest.mark.asyncio

USER, ASSISTANT = MessageRole.USER, MessageRole.ASSISTANT


async def _records(w: World, run_id: str, count: int) -> list[Evidence]:
    found: list[Evidence] = []
    for i in range(count):
        evidence, _ = await w.query(run_id, values={"min_amount": i + 1})
        found.append(evidence)
        w.tick()
    return found


def _ids(pairs: tuple[tuple[str, object], ...]) -> set[str]:
    return {key for key, _ in pairs}


async def test_count_limit_omits_from_prompt_but_not_from_standing() -> None:
    w = World()
    old = await _records(w, w.new_run(), 4)
    run = w.new_run()
    new = await _records(w, run, 4)
    ctx = await w.builder.build(A, run, "September revenue")
    shown = {d.evidence_id for d in ctx.evidence}
    assert len(shown) == 6 and ctx.omissions.evidence_over_budget == 2
    everything = {e.evidence_id for e in old + new}
    assert _ids(ctx.current_evidence) == everything
    assert dict(ctx.current_evidence) == {e.evidence_id: e.version for e in old + new}


async def test_size_limit_omits_from_prompt_but_not_from_standing() -> None:
    w = World(budget=ContextBudget(max_tokens=500, max_rows_per_evidence=1))
    run = w.new_run()
    records = await _records(w, run, 5)
    # A long request leaves room for only part of the evidence.
    ctx = await w.builder.build(A, run, "Next " + "y" * 1_600)
    assert len(ctx.evidence) < 5 and ctx.omissions.evidence_over_budget >= 1
    assert _ids(ctx.current_evidence) == {e.evidence_id for e in records}


async def test_run_records_beyond_the_scan_are_still_judged() -> None:
    w = World(budget=ContextBudget(evidence_scan=2))
    run = w.new_run()
    records = await _records(w, run, 5)
    ctx = await w.builder.build(A, run, "Next")
    # Only two records are scanned, but every record linked to the run is
    # loaded and judged, so none is "unknown".
    assert _ids(ctx.current_evidence) == {e.evidence_id for e in records}


async def test_invalidated_and_out_of_scope_records_leave_the_standing() -> None:
    w = World()
    run = w.new_run()
    records = await _records(w, run, 3)
    await w.store.invalidate_dependent_findings(
        EXEC_A, "s-a", "metric_definition:revenue"
    )
    ctx = await w.builder.build(A, run, "Next")
    assert ctx.current_evidence == ()

    w2 = World()
    run2 = w2.new_run()
    await _records(w2, run2, 2)
    w2.set_products(EXEC_A, NARROW)
    assert (await w2.builder.build(A, run2, "Next")).current_evidence == ()
    assert records  # recorded before invalidation


async def test_history_eviction_keeps_message_fingerprints() -> None:
    w = World(budget=ContextBudget(max_history_messages=3))
    run = w.new_run()
    for i in range(3):
        w.say(USER, f"question {i}", run)
        w.tick()
    first = await w.builder.build(A, run, "Next")
    shown = {m.message_id: m.fingerprint for m in first.history}
    assert len(shown) == 3
    for i in range(3, 6):
        w.say(USER, f"question {i}", run)
        w.tick()
    later = await w.builder.build(A, run, "Next")
    current = dict(later.current_history)
    assert not set(shown) & {m.message_id for m in later.history}
    # Evicted for capacity, still valid with the same fingerprint.
    assert all(current[k] == v for k, v in shown.items())


async def test_withheld_history_leaves_the_standing() -> None:
    w = World()
    r1 = w.new_run()
    await w.query(r1)
    w.tick()
    answer = w.say(ASSISTANT, "Top spender spent 130.0", r1)
    run = w.new_run()
    before = await w.builder.build(A, run, "Next")
    assert answer.message_id in dict(before.current_history)
    await w.store.invalidate_dependent_findings(
        EXEC_A, "s-a", "metric_definition:revenue"
    )
    after = await w.builder.build(A, run, "Next")
    assert answer.message_id not in dict(after.current_history)
