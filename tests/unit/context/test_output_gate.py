"""Output privacy gate: nothing leaves without a check under current authority."""

from __future__ import annotations

import base64
from decimal import Decimal

import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.output_privacy import (
    OutputDestination,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.disclosure import MASK, DisclosureKind, ProtectedTerm
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.context.support import NARROW, A, B, World, references_of
from tests.unit.privacy.support import EXEC_A

pytestmark = pytest.mark.asyncio

DISPLAY = OutputDestination.DISPLAY
REPORT = OutputDestination.REPORT
MEMORY = OutputDestination.MEMORY
PROGRESS = OutputDestination.PROGRESS


def section(text: str, *cited: str) -> OutputSection:
    return OutputSection("answer", text, cited)


async def test_positive_demographic_answer_with_permitted_reference_is_released() -> (
    None
):
    w = World()
    r1 = w.new_run()
    evidence, released = await w.query(r1)
    ref = references_of(released)[0]
    text = (
        f"Women aged 35-39 in CA (US) led spend at 1,234.50; customers aged 25-29 "
        f"in Texas followed; {ref} bought twice. Revenue means completed item "
        "sales, in USD, UTC."
    )
    (out,) = await w.gate.check(A, r1, [section(text, evidence.evidence_id)], REPORT)
    assert out.text == text and out.masked == () and out.masked_spans == 0
    assert out.cited_evidence == (evidence.evidence_id,)


@pytest.mark.parametrize(
    ("smuggled", "kind"),
    [
        ("dave@example.invalid", DisclosureKind.EMAIL),
        ("dave (at) example (dot) invalid", DisclosureKind.EMAIL),
        ("+1 (555) 123-4567", DisclosureKind.PHONE),
        ("the customer named Dave Secret", DisclosureKind.PERSON_NAME),
        ("Alice Private", DisclosureKind.PERSON_NAME),  # lexicon (source names)
        ("4 High St", DisclosureKind.STREET_ADDRESS),
        ("a 93 year old", DisclosureKind.EXACT_AGE),
        ("customer id 40", DisclosureKind.RAW_IDENTIFIER),
        (base64.b64encode(b"dave@example.invalid").decode(), "encoded"),
        ("dave%40example.invalid", "encoded"),
        (b"dave@example.invalid".hex(), "encoded"),
    ],
)
async def test_pii_smuggled_in_model_text_is_masked_for_display_and_blocks_reports(
    smuggled: str, kind: object
) -> None:
    w = World(lexicon=(ProtectedTerm("Alice Private"),))
    r1 = w.new_run()
    await w.query(r1)
    policy = await w.gate.policy_for_run(A, r1)
    text = f"The top buyer is {smuggled}; spend rose in September."
    out = w.gate.release(policy, section(text), DISPLAY)
    assert smuggled not in out.text and MASK in out.text
    assert out.text.endswith("spend rose in September.")
    if isinstance(kind, DisclosureKind):
        assert kind in out.masked
    progress = w.gate.release(policy, section(text), PROGRESS)
    assert smuggled not in progress.text
    for destination in (REPORT, MEMORY):
        with pytest.raises(OutputWithheld) as caught:
            w.gate.release(policy, section(text), destination)
        assert caught.value.reason == "personal_data" and caught.value.correctable
        assert smuggled not in caught.value.message
        assert smuggled not in repr(caught.value)


async def test_names_the_user_typed_cannot_be_echoed_later() -> None:
    w = World()
    r1 = w.new_run()
    w.say(MessageRole.USER, "What did the customer named Erin Private order?", r1)
    policy = await w.gate.policy_for_run(A, r1)
    out = w.gate.release(policy, section("Erin Private ordered twice."), DISPLAY)
    assert "Erin" not in out.text


async def test_scope_narrowed_blocks_old_figures_references_and_citations() -> None:
    w = World()
    r1 = w.new_run()
    evidence, released = await w.query(r1)
    wide_refs = references_of(released)
    big = await w.external(r1, (("Beta total", Decimal("1234567.89")),))
    stale_policy = await w.gate.policy_for_run(A, r1)
    assert w.gate.release(stale_policy, section("Beta made 1.23M"), DISPLAY)

    w.set_products(EXEC_A, NARROW)
    r2 = w.new_run()
    policy = await w.gate.policy_for_run(A, r2)
    assert policy.usable_evidence == frozenset()

    cases = {
        "out_of_scope_figure": section("Beta made 1.23M last month"),
        "unknown_reference": section(f"{wide_refs[0]} was the top buyer"),
        "unavailable_evidence": section("See the earlier table", evidence.evidence_id),
    }
    for reason, item in cases.items():
        with pytest.raises(OutputWithheld) as caught:
            w.gate.release(policy, item, DISPLAY)
        assert caught.value.reason == reason
        assert caught.value.code is ToolErrorCode.ACCESS_DENIED
    with pytest.raises(OutputWithheld):
        w.gate.release(policy, section("total 1,234,567.89"), DISPLAY)
    # Figures not tied to withheld evidence are fine.
    assert w.gate.release(policy, section("Up 12% to 2,000.00"), DISPLAY)
    assert big.evidence_id in policy.withdrawn_evidence
    # The gate check re-resolves authority itself.
    with pytest.raises(OutputWithheld):
        await w.gate.check(A, r2, [section("Beta made 1.23M")], DISPLAY)


async def test_stale_or_invalidated_citations_and_other_executives_refs_block() -> None:
    w = World()
    r1 = w.new_run()
    evidence, _ = await w.query(r1)
    rb = w.new_run(B.executive_id, "s-b")
    _, released_b = await w.query(rb, principal=B)
    ref_b = references_of(released_b)[0]
    await w.store.invalidate_dependent_findings(
        EXEC_A, "s-a", "metric_definition:revenue"
    )
    policy = await w.gate.policy_for_run(A, r1)
    with pytest.raises(OutputWithheld) as caught:
        w.gate.release(policy, section("As shown", evidence.evidence_id), REPORT)
    assert caught.value.reason == "unavailable_evidence"
    with pytest.raises(OutputWithheld) as caught:
        w.gate.release(policy, section(f"{ref_b} spent most"), DISPLAY)
    assert caught.value.reason == "unknown_reference"
    with pytest.raises(OutputWithheld) as caught:
        w.gate.release(policy, section("cus_0000000000000000000000ff"), DISPLAY)
    assert caught.value.reason == "unknown_reference"


async def test_secrets_and_memory_references_fail_closed() -> None:
    w = World()
    r1 = w.new_run()
    _, released = await w.query(r1)
    ref = references_of(released)[0]
    policy = await w.gate.policy_for_run(A, r1)
    with pytest.raises(OutputWithheld) as caught:
        w.gate.release(policy, section("bound @_policy_ref_inner"), DISPLAY)
    assert caught.value.reason == "internal_secret" and not caught.value.correctable
    # References are per-executive pseudonyms: never promoted to memory.
    with pytest.raises(OutputWithheld):
        w.gate.release(policy, section(f"Watch {ref}"), MEMORY)
    assert w.gate.release(policy, section(f"Watch {ref}"), DISPLAY).masked == ()


async def test_detector_failure_withholds_the_section() -> None:
    w = World()
    r1 = w.new_run()
    policy = await w.gate.policy_for_run(A, r1)
    broken = OutputSection("answer", None)  # type: ignore[arg-type]
    with pytest.raises(OutputWithheld) as caught:
        w.gate.release(policy, broken, DISPLAY)
    assert caught.value.reason == "check_failed"
    assert caught.value.code is ToolErrorCode.INTERNAL_ERROR


async def test_gate_rejects_runs_of_other_executives() -> None:
    w = World()
    rb = w.new_run(B.executive_id, "s-b")
    with pytest.raises(AccessDenied):
        await w.gate.check(A, rb, [section("hello")], DISPLAY)


async def test_all_or_nothing_release() -> None:
    w = World()
    r1 = w.new_run()
    with pytest.raises(OutputWithheld) as caught:
        await w.gate.check(
            A,
            r1,
            [
                OutputSection("summary", "Revenue grew."),
                OutputSection("details", "Ask carol@example.invalid", ()),
            ],
            REPORT,
        )
    assert caught.value.section == "details"
