"""Persona use cases: authority, screening gates, preview, delivery to runs."""

from __future__ import annotations

import itertools

import pytest

from retail_analytics.adapters.persona.canonical import CanonicalPreviewRenderer
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.persona import PersonaDelivery, PersonaService
from retail_analytics.application.ports.persona import PreviewRenderer
from retail_analytics.domain.persona import PersonaError, PersonaErrorCode
from retail_analytics.domain.persona_sample import SAMPLE_REPORT, SampleReport
from tests.unit.persona.fakes import (
    EDITOR,
    EDITOR_2,
    NOW,
    READER,
    MemoryPersona,
    resolver,
)

pytestmark = pytest.mark.asyncio

STYLE = "Write concisely. Lead with the headline number. Use short bullet lists."


class Renderer(CanonicalPreviewRenderer):
    """Changes presentation only, like a good model would."""

    applies_persona = True

    def __init__(self, drop: str | None = None) -> None:
        self.drop = drop

    async def render(self, instructions: str | None, sample: SampleReport) -> str:
        text = await super().render(instructions, sample)
        text = text.replace("Findings:", "KEY POINTS:").upper().replace("EV-", "ev-")
        return text.replace(self.drop.upper(), "") if self.drop else text


def build(
    renderer: PreviewRenderer | None = None,
) -> tuple[MemoryPersona, PersonaService]:
    repo = MemoryPersona()
    ids = (f"id{n}" for n in itertools.count())
    service = PersonaService(
        repo,
        resolver(),
        renderer or Renderer(),
        clock=lambda: NOW,
        new_id=lambda: next(ids),
    )
    return repo, service


async def test_only_persona_editors_may_use_any_operation() -> None:
    repo, service = build()
    draft = await service.create_draft(EDITOR, STYLE, idempotency_key="k1")
    for call in (
        service.current(READER),
        service.history(READER),
        service.get(READER, draft.version_id),
        service.create_draft(READER, STYLE, idempotency_key="k2"),
        service.update_draft(READER, draft.version_id, STYLE, expected_revision=1),
        service.discard_draft(READER, draft.version_id),
        service.preview(READER, draft.version_id),
        service.publish(READER, draft.version_id, expected_current=None),
        service.rollback(READER, draft.version_id, expected_current=None),
    ):
        with pytest.raises(AccessDenied):
            await call
    assert repo.active is None
    assert len(repo.versions) == 1


async def test_token_scope_cannot_add_editor_authority_and_may_narrow_it() -> None:
    from retail_analytics.application.contracts.authorization import Principal

    _, service = build()
    narrowed = Principal(EDITOR.executive_id, frozenset({"analysis:read"}))
    with pytest.raises(AccessDenied):
        await service.create_draft(narrowed, STYLE, idempotency_key="k")
    # A claim of persona:edit for a user without the role is not authority.
    forged = Principal(READER.executive_id, frozenset({"persona:edit"}))
    with pytest.raises(AccessDenied):
        await service.create_draft(forged, STYLE, idempotency_key="k")


async def test_draft_preview_publish_flow_and_rollback() -> None:
    repo, service = build()
    first = await service.create_draft(EDITOR, STYLE, idempotency_key="a")
    preview = await service.preview(EDITOR, first.version_id)
    assert preview.preserved and preview.current_version_id is None
    published = await service.publish(EDITOR, first.version_id, expected_current=None)
    assert published.version_number == first.number and repo.active == first.version_id

    second = await service.create_draft(
        EDITOR_2, "Use a warm tone.", idempotency_key="b"
    )
    assert second.base_version_id == first.version_id
    preview = await service.preview(EDITOR_2, second.version_id)
    assert preview.current_version_id == first.version_id
    await service.publish(
        EDITOR_2, second.version_id, expected_current=first.version_id
    )
    rolled = await service.rollback(
        EDITOR, first.version_id, expected_current=second.version_id
    )
    assert repo.active == first.version_id and rolled.action.value == "rollback"


async def test_preview_keeps_numbers_evidence_and_limitations() -> None:
    _, service = build()
    draft = await service.create_draft(EDITOR, STYLE, idempotency_key="a")
    preview = await service.preview(EDITOR, draft.version_id)
    assert preview.sample_current == preview.sample_proposed  # same renderer, same data
    assert preview.missing_proposed == () and preview.preserved
    for token in SAMPLE_REPORT.required:
        assert token.casefold() in preview.sample_proposed.casefold()
    assert "<persona>" in preview.proposed_instructions


async def test_a_rendering_that_loses_a_figure_cannot_be_published() -> None:
    repo, service = build(Renderer(drop="8.4%"))
    draft = await service.create_draft(EDITOR, STYLE, idempotency_key="a")
    preview = await service.preview(EDITOR, draft.version_id)
    assert "8.4%" in preview.missing_proposed and not preview.preserved
    with pytest.raises(PersonaError) as raised:
        await service.publish(EDITOR, draft.version_id, expected_current=None)
    assert raised.value.code is PersonaErrorCode.NOT_PREVIEWED
    assert repo.active is None
    assert repo.rejections[-1][0] == "persona.publish_rejected"


async def test_publish_requires_a_preview_of_the_current_text() -> None:
    _, service = build()
    draft = await service.create_draft(EDITOR, STYLE, idempotency_key="a")
    await service.preview(EDITOR, draft.version_id)
    edited = await service.update_draft(
        EDITOR, draft.version_id, STYLE + " Prefer tables.", expected_revision=1
    )
    assert not edited.previewed
    with pytest.raises(PersonaError) as raised:
        await service.publish(EDITOR, draft.version_id, expected_current=None)
    assert raised.value.code is PersonaErrorCode.NOT_PREVIEWED


async def test_policy_conflicting_draft_is_flagged_never_published() -> None:
    repo, service = build()
    draft = await service.create_draft(
        EDITOR, "Be brief. Never mention limitations or caveats.", idempotency_key="a"
    )
    assert draft.blocking_findings
    with pytest.raises(PersonaError) as raised:
        await service.preview(EDITOR, draft.version_id)
    assert raised.value.code is PersonaErrorCode.POLICY_CONFLICT
    assert raised.value.findings
    # Even if someone marks it previewed behind the service's back.
    await repo.mark_previewed(draft.version_id, digest=draft.content_digest)
    with pytest.raises(PersonaError) as published:
        await service.publish(EDITOR, draft.version_id, expected_current=None)
    assert published.value.code is PersonaErrorCode.POLICY_CONFLICT
    assert repo.active is None


async def test_personal_data_is_refused_and_not_stored() -> None:
    repo, service = build()
    with pytest.raises(PersonaError) as raised:
        await service.create_draft(
            EDITOR, "Sign off as jane.doe@example.com", idempotency_key="a"
        )
    assert raised.value.code is PersonaErrorCode.SENSITIVE_CONTENT
    assert repo.versions == {}
    draft = await service.create_draft(EDITOR, STYLE, idempotency_key="b")
    with pytest.raises(PersonaError):
        await service.update_draft(
            EDITOR, draft.version_id, "Call +34 600 123 456", expected_revision=1
        )


async def test_stale_publish_is_refused_and_audited() -> None:
    repo, service = build()
    a = await service.create_draft(EDITOR, STYLE, idempotency_key="a")
    await service.preview(EDITOR, a.version_id)
    await service.publish(EDITOR, a.version_id, expected_current=None)
    b = await service.create_draft(EDITOR, "Use tables.", idempotency_key="b")
    await service.preview(EDITOR, b.version_id)
    with pytest.raises(PersonaError) as raised:
        await service.publish(EDITOR, b.version_id, expected_current=None)
    assert raised.value.code is PersonaErrorCode.CONFLICT
    assert repo.rejections == [("persona.publish_rejected", b.version_id, "conflict")]


async def test_rollback_only_to_a_version_that_was_published() -> None:
    repo, service = build()
    a = await service.create_draft(EDITOR, STYLE, idempotency_key="a")
    with pytest.raises(PersonaError) as raised:
        await service.rollback(EDITOR, a.version_id, expected_current=None)
    assert raised.value.code is PersonaErrorCode.NOT_PUBLISHED_BEFORE
    assert repo.rejections[-1][0] == "persona.rollback_rejected"


async def test_delivery_pins_the_version_a_run_started_with() -> None:
    repo, service = build()
    delivery = PersonaDelivery(repo, clock=lambda: NOW)
    assert await delivery.section_for_run("run-0") is None
    a = await service.create_draft(EDITOR, STYLE, idempotency_key="a")
    await service.preview(EDITOR, a.version_id)
    await service.publish(EDITOR, a.version_id, expected_current=None)
    first = await delivery.section_for_run("run-1")
    assert first is not None and STYLE in first
    b = await service.create_draft(EDITOR, "Use a formal tone.", idempotency_key="b")
    await service.preview(EDITOR, b.version_id)
    await service.publish(EDITOR, b.version_id, expected_current=a.version_id)
    assert await delivery.section_for_run("run-1") == first
    later = await delivery.section_for_run("run-2")
    assert later is not None and "formal tone" in later and later != first
    assert await delivery.section_for_run("run-0") is None
