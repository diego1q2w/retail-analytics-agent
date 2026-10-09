"""Numbered answer sources at read time (real resolver, evidence and gate):
recognized only for usable evidence the run used, rechecked on every read."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from retail_analytics.application import citations as citations_module
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.citations import (
    UNAVAILABLE_DESCRIPTION,
    CitationSources,
    describe_source,
)
from retail_analytics.application.conversations import ConversationService
from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    EvidenceContent,
    EvidenceKind,
    Provenance,
    ReportSource,
)
from retail_analytics.domain.periods import DateWindow
from retail_analytics.domain.runs import RunStatus
from retail_analytics.interfaces.http.schemas import RunDetailOut
from tests.unit.context.support import BRAND_SQL, NARROW, A, B, World
from tests.unit.http.test_conversations import Events, Inputs, Reader, Sessions
from tests.unit.privacy.support import EXEC_A, EXEC_B

pytestmark = pytest.mark.asyncio


class Env:
    def __init__(self) -> None:
        self.world = World(id_prefix="evd_")
        self.reader = Reader(self.world)
        self.service = ConversationService(
            resolver=self.world.resolver,
            guard=self.world.guard,
            sessions=Sessions(self.world),  # type: ignore[arg-type]
            runs=self.world.records,  # type: ignore[arg-type]
            inputs=Inputs(),  # type: ignore[arg-type]
            events=Events(),
            reader=self.reader,
            gate=self.world.gate,
            citations=CitationSources(
                resolver=self.world.resolver,
                evidence=self.world.evidence,
                gate=self.world.gate,
            ),
        )

    def answer(
        self, run_id: str, text: str, status: RunStatus = RunStatus.COMPLETED
    ) -> None:
        run = self.world.records.runs[run_id]
        self.world.records.runs[run_id] = replace(run, status=status)
        self.reader.answers[run_id] = self.world.say(
            MessageRole.ASSISTANT, text, run_id, session=run.session_id
        )


@pytest.fixture
def env() -> Env:
    return Env()


async def test_repeated_citations_share_first_use_numbers(env: Env) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    brand, _ = await env.world.query(run, BRAND_SQL)
    text = (
        f"Brand revenue led [{brand.evidence_id}]; spend held "
        f"[{spend.evidence_id}, {brand.evidence_id}]. Again [{brand.evidence_id}]."
    )
    env.answer(run, text)

    view = await env.service.run_view(A, run)
    assert view.answer is not None and not view.answer.withheld
    # The released text keeps the stored IDs; numbering is metadata.
    assert view.answer.text == text
    cited = view.answer.citations
    assert [(c.number, c.label, c.evidence_id) for c in cited] == [
        (1, "1", brand.evidence_id),
        (2, "2", spend.evidence_id),
    ]
    assert cited[0].description.startswith("Query result; definition basis: ")
    assert "September 2026 (UTC, by ordered date)" in cited[0].description
    assert "grouped by brand" in cited[0].description
    # No SQL, parameters or rows in a description.
    assert "SELECT" not in cited[0].description
    assert "sale_amount" not in cited[1].description

    again = await env.service.run_view(A, run)
    assert again.answer is not None and again.answer.citations == cited
    out = RunDetailOut.of_view(again).model_dump()
    assert out["answer"]["citations"][0]["evidence_id"] == brand.evidence_id
    assert out["answer"]["citations"][0]["label"] == "1"


async def test_partial_answers_get_the_same_mapping(env: Env) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    env.answer(run, f"Evidence: {spend.evidence_id}", RunStatus.PARTIAL)
    view = await env.service.run_view(A, run)
    assert view.answer is not None
    assert [c.evidence_id for c in view.answer.citations] == [spend.evidence_id]


async def test_another_executive_gets_nothing(env: Env) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    env.answer(run, f"Spend [{spend.evidence_id}].")
    with pytest.raises(AccessDenied):
        await env.service.run_view(B, run)


async def test_unlinked_foreign_and_invented_ids_are_not_sources(env: Env) -> None:
    earlier = env.world.new_run()
    unlinked, _ = await env.world.query(earlier)
    other = env.world.new_run(executive=EXEC_B, session="s-b")
    foreign, _ = await env.world.query(other, BRAND_SQL, principal=B)
    run = env.world.new_run()
    own, _ = await env.world.query(run, BRAND_SQL)
    text = (
        f"Own [{own.evidence_id}], earlier [{unlinked.evidence_id}], "
        f"foreign [{foreign.evidence_id}], invented [evd_ffffffff]."
    )
    env.answer(run, text)
    view = await env.service.run_view(A, run)
    assert view.answer is not None and not view.answer.withheld
    assert [c.evidence_id for c in view.answer.citations] == [own.evidence_id]


async def test_narrowed_scope_withholds_answer_and_sources(env: Env) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    env.answer(run, f"Spend [{spend.evidence_id}].")
    before = await env.service.run_view(A, run)
    assert before.answer is not None and before.answer.citations

    env.world.set_products(EXEC_A, NARROW)
    after = await env.service.run_view(A, run)
    assert after.answer is not None and after.answer.withheld
    assert after.answer.citations == ()
    assert spend.evidence_id not in after.answer.text


async def test_superseded_evidence_is_listed_as_not_current(env: Env) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    env.answer(run, f"Spend [{spend.evidence_id}].")
    await env.world.store.invalidate_dependent_findings(
        EXEC_A, "s-a", "metric_definition:revenue"
    )
    view = await env.service.run_view(A, run)
    assert view.answer is not None and not view.answer.withheld
    (source,) = view.answer.citations
    assert not source.current
    assert "superseded" in source.description


async def test_missing_metadata_is_described_as_unknown(env: Env) -> None:
    run = env.world.new_run()
    old = await env.world.external(run, (("a", 10),))
    env.answer(run, f"Total 10 [{old.evidence_id}].")
    view = await env.service.run_view(A, run)
    assert view.answer is not None
    (source,) = view.answer.citations
    assert "definition basis not recorded" in source.description
    assert "period not recorded" in source.description
    # Provider notes are not echoed.
    assert "fixture" not in source.description


async def test_unbuildable_description_falls_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    env.answer(run, f"Spend [{spend.evidence_id}].")

    def broken(*_: object) -> str:
        raise RuntimeError("bad metadata")

    monkeypatch.setattr(citations_module, "describe_source", broken)
    view = await env.service.run_view(A, run)
    assert view.answer is not None
    (source,) = view.answer.citations
    assert source.description == UNAVAILABLE_DESCRIPTION


async def test_numbered_prose_switches_to_prefixed_labels(env: Env) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    env.answer(run, f"See step [1] above; spend [{spend.evidence_id}].")
    view = await env.service.run_view(A, run)
    assert view.answer is not None
    assert [c.label for c in view.answer.citations] == ["S1"]


async def test_service_without_citations_is_unchanged() -> None:
    world = World(id_prefix="evd_")
    reader = Reader(world)
    service = ConversationService(
        resolver=world.resolver,
        guard=world.guard,
        sessions=Sessions(world),  # type: ignore[arg-type]
        runs=world.records,  # type: ignore[arg-type]
        inputs=Inputs(),  # type: ignore[arg-type]
        events=Events(),
        reader=reader,
        gate=world.gate,
    )
    run = world.new_run()
    spend, _ = await world.query(run)
    world.records.runs[run] = replace(
        world.records.runs[run], status=RunStatus.COMPLETED
    )
    reader.answers[run] = world.say(
        MessageRole.ASSISTANT, f"Spend [{spend.evidence_id}].", run
    )
    view = await service.run_view(A, run)
    assert view.answer is not None and view.answer.citations == ()


# --- descriptions from trusted metadata --------------------------------------


async def test_saved_report_source_keeps_its_disclosure(env: Env) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    source = ReportSource("rep-1", 3, "Q3 review", datetime(2026, 10, 9, tzinfo=UTC))
    text = describe_source(EvidenceStanding(spend, None, source), {})
    assert text.startswith('From saved report "Q3 review" (v3), computed ')
    assert "historical snapshot, not current data" in text


async def test_conversion_keeps_rate_provenance(env: Env) -> None:
    run = env.world.new_run()
    spend, _ = await env.world.query(run)
    notes = (
        ("kind", "currency_conversion"),
        ("source_currency", ""),
        ("display_currency", "EUR"),
        ("rate", "0.9213"),
        ("rate_source", "ECB reference rates"),
        ("rate_date", "2026-10-08"),
        ("rate_method", "daily reference"),
    )
    content = EvidenceContent(
        kind=EvidenceKind.DERIVED,
        subject_key="derived:x",
        analysis=AnalysisStamp(
            catalog_version=1,
            policy_version=1,
            definitions=frozenset(),
            preference_fingerprint="f",
            period=DateWindow(date(2026, 9, 1), date(2026, 9, 15)),
        ),
        provenance=Provenance(notes=notes),
        table=spend.content.table,
        grain=spend.content.grain,
        derived_from=(spend.evidence_id,),
    )
    converted = replace(spend, evidence_id="evd_conv", content=content)
    text = describe_source(
        EvidenceStanding(converted, None), {spend.evidence_id: "1", "evd_conv": "2"}
    )
    assert text.startswith("Currency conversion of [1] to EUR; rate 0.9213 (")
    assert "ECB reference rates, rate date 2026-10-08, daily reference" in text
    assert "source currency not verified" in text
