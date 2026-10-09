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
from retail_analytics.application.contracts.investigations import ModelStep
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
