"""Provider history is reusable only while its source context remains valid."""

from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import ModelRequestParameters
from temporalio.exceptions import ApplicationError

from retail_analytics.adapters.agent import investigator
from retail_analytics.adapters.temporal import agent
from retail_analytics.application.contracts.investigations import (
    ContextRestartCause,
    ContextStanding,
    ModelStep,
)
from tests.unit.test_investigation_models import MESSAGES

pytestmark = pytest.mark.asyncio


def _guarded(
    monkeypatch: pytest.MonkeyPatch, steps: object, provider: object
) -> investigator.GuardedModel:
    """The Temporal path's guarded model over controlled steps and provider."""
    monkeypatch.setattr(
        investigator,
        "current_deps",
        lambda: investigator.InvestigationDeps(run_id="run"),
    )
    services = cast(
        investigator.AgentServices, SimpleNamespace(steps=steps, model=provider)
    )
    return agent.TemporalGuardedModel(investigator.AgentBinding(services))


@pytest.mark.parametrize("change", ["authority", "removed", "revised", "unknown"])
async def test_stale_history_never_reaches_provider(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    step = ModelStep("safe context", frozenset(), "scope-v1", (("e1", 1),))
    steps = SimpleNamespace(prepare_model_step=AsyncMock(return_value=step))
    provider = SimpleNamespace(
        request=AsyncMock(
            return_value=ModelResponse(
                parts=[
                    TextPart("Previously restricted customers contribute 12.5%."),
                    ToolCallPart("query", {"filter": "restricted qualitative finding"}),
                ]
            )
        )
    )
    model = _guarded(monkeypatch, steps, provider)
    response = await model.request(MESSAGES, None, ModelRequestParameters())
    # Temporal serializes these messages; validation must survive reconstruction.
    history = ModelMessagesTypeAdapter.validate_json(
        ModelMessagesTypeAdapter.dump_json([*MESSAGES, response])
    )
    if change == "authority":
        steps.prepare_model_step.return_value = replace(step, history_key="scope-v2")
    elif change == "removed":
        steps.prepare_model_step.return_value = replace(step, evidence_versions=())
    elif change == "revised":
        steps.prepare_model_step.return_value = replace(
            step, evidence_versions=(("e1", 2),)
        )
    else:
        assert isinstance(history[-1], ModelResponse)
        history[-1].metadata = None
    with pytest.raises(ApplicationError) as caught:
        await model.request(history, None, ModelRequestParameters())
    assert agent.is_context_changed(caught.value)
    assert caught.value.non_retryable
    assert provider.request.await_count == 1
    await model.request(MESSAGES, None, ModelRequestParameters())
    assert provider.request.await_count == 2
    assert "restricted" not in str(provider.request.call_args.args[0])


async def test_new_evidence_preserves_valid_tool_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step = ModelStep("safe context", frozenset(), "scope-v1", (("e1", 1),))
    steps = SimpleNamespace(prepare_model_step=AsyncMock(return_value=step))
    provider = SimpleNamespace(
        request=AsyncMock(return_value=ModelResponse(parts=[TextPart("safe")]))
    )
    model = _guarded(monkeypatch, steps, provider)
    response = await model.request(MESSAGES, None, ModelRequestParameters())
    steps.prepare_model_step.return_value = replace(
        step, evidence_versions=(("e1", 1), ("e2", 1))
    )
    await model.request([*MESSAGES, response], None, ModelRequestParameters())
    assert response in provider.request.call_args.args[0]


def _responded(messages: dict[str, str], evidence: dict[str, int]) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart("earlier")],
        metadata={
            "retail_context": {
                "key": "scope-v1",
                "evidence": evidence,
                "messages": messages,
                "parts": {},
            }
        },
    )


STANDING = ContextStanding(
    evidence=(("e1", 1), ("e2", 1)),
    messages=(("m1", "f1"), ("m2", "f2")),
)


@pytest.mark.parametrize(
    ("shown", "cause"),
    [
        # m1 and e1 were shown earlier and are now only left out of the prompt.
        (({"m1": "f1"}, {"e1": 1}), None),
        (({"m1": "f-other"}, {"e1": 1}), ContextRestartCause.HISTORY_CHANGED),
        (({"m3": "f3"}, {"e1": 1}), ContextRestartCause.HISTORY_CHANGED),
        (({"m1": "f1"}, {"e1": 2}), ContextRestartCause.EVIDENCE_INVALIDATED),
        (({"m1": "f1"}, {"e9": 1}), ContextRestartCause.EVIDENCE_INVALIDATED),
    ],
)
async def test_validity_is_judged_by_standing_not_by_selection(
    shown: tuple[dict[str, str], dict[str, int]], cause: ContextRestartCause | None
) -> None:
    step = ModelStep(
        "context",
        frozenset(),
        "scope-v1",
        (("e2", 1),),
        (("m2", "f2"),),
        STANDING,
    )
    history = [*MESSAGES, _responded(*shown)]
    assert investigator.restart_cause(history, step) is cause
    # Without a standing, only the current selection counts (strict).
    if cause is None:
        assert investigator.restart_cause(history, replace(step, standing=None))


async def test_missing_or_old_provenance_fails_closed() -> None:
    step = ModelStep("context", frozenset(), "scope-v1", (), (), STANDING)
    old = ModelResponse(
        parts=[TextPart("earlier")],
        metadata={"retail_context": {"key": "scope-v1", "evidence": {}}},
    )
    for response in (old, ModelResponse(parts=[TextPart("earlier")])):
        assert (
            investigator.restart_cause([*MESSAGES, response], step)
            is ContextRestartCause.PROVENANCE_MISSING
        )


async def test_changed_key_names_the_changed_part() -> None:
    parts = (("authority", "a1"), ("request", "q1"), ("preferences", "p1"))
    step = ModelStep(
        "context",
        frozenset(),
        "scope-v2",
        (),
        (),
        replace(STANDING, key_parts=parts),
    )
    earlier = _responded({}, {})
    assert earlier.metadata is not None
    earlier.metadata["retail_context"]["parts"] = {
        "authority": "a1",
        "request": "q0",
        "preferences": "p1",
    }
    assert (
        investigator.restart_cause([*MESSAGES, earlier], step)
        is ContextRestartCause.REQUEST_CHANGED
    )
    earlier.metadata["retail_context"]["parts"] = {}
    assert (
        investigator.restart_cause([*MESSAGES, earlier], step)
        is ContextRestartCause.CONTEXT_CHANGED
    )
