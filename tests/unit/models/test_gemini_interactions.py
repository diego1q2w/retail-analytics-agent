"""The Gemini Interactions adapter against an HTTP stub (no network)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from retail_analytics.adapters.models.gemini_interactions import (
    FOREIGN_CALL_SIGNATURE,
    PROVIDER,
)
from tests.unit.models import stubs

pytestmark = pytest.mark.asyncio

TOOL = ToolDefinition(
    name="execute_analysis",
    description="Run an analysis.",
    parameters_json_schema={
        "type": "object",
        "properties": {"sql": {"type": "string"}},
        "required": ["sql"],
        "additionalProperties": False,
    },
)
ANSWER = ToolDefinition(
    name="final_result_AnswerOutput",
    description="The answer.",
    parameters_json_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
PARAMS = ModelRequestParameters(
    function_tools=[TOOL],
    output_tools=[ANSWER],
    output_mode="tool",
    allow_text_output=False,
)
QUESTION: list[ModelMessage] = [
    ModelRequest(
        parts=[SystemPromptPart("Policy."), UserPromptPart("Sales last month?")]
    )
]


async def test_request_is_stateless_streamed_and_keyed_in_a_header() -> None:
    recorder = stubs.Recorder([stubs.Reply(events=stubs.gemini_text("Hello."))])
    model = stubs.gemini(recorder)

    await model.request(QUESTION, None, PARAMS)

    body = recorder.requests[0]
    assert body["model"] == "gemini-3.8-flash"
    assert body["store"] is False and body["stream"] is True
    assert body["system_instruction"] == "Policy."
    assert body["input"] == [
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "Sales last month?"}],
        }
    ]
    assert [t["name"] for t in body["tools"]] == [TOOL.name, ANSWER.name]
    assert body["generation_config"]["tool_choice"] == "any"
    assert recorder.urls[0].endswith("/v1beta/interactions")
    assert stubs.GEMINI_KEY not in recorder.urls[0]
    assert recorder.headers[0]["x-goog-api-key"] == stubs.GEMINI_KEY


async def test_tool_catalog_is_exactly_the_given_definitions() -> None:
    recorder = stubs.Recorder([stubs.Reply(events=stubs.gemini_text("Hi."))])
    only_answer = ModelRequestParameters(
        output_tools=[ANSWER], output_mode="tool", allow_text_output=True
    )
    await stubs.gemini(recorder).request(QUESTION, None, only_answer)
    body = recorder.requests[0]
    assert [t["name"] for t in body["tools"]] == [ANSWER.name]
    assert body["generation_config"]["tool_choice"] == "auto"


async def test_streamed_call_keeps_signatures_and_reported_usage() -> None:
    recorder = stubs.Recorder(
        [stubs.Reply(events=stubs.gemini_call("execute_analysis", {"sql": "SELECT 1"}))]
    )
    response = await stubs.gemini(recorder).request(QUESTION, None, PARAMS)

    call = next(p for p in response.parts if isinstance(p, ToolCallPart))
    assert call.tool_name == "execute_analysis"
    assert call.args_as_dict() == {"sql": "SELECT 1"}
    assert call.tool_call_id == "call_1"
    assert call.provider_details == {"signature": "call-sig"}
    thought = next(p for p in response.parts if isinstance(p, ThinkingPart))
    assert thought.signature == "thought-sig" and thought.content == ""
    assert response.provider_name == PROVIDER
    assert response.finish_reason == "tool_call"
    # Input + tool-use prompts; output + thinking.
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 25


async def test_history_round_trip_echoes_own_signatures_only() -> None:
    history: list[ModelMessage] = [
        *QUESTION,
        ModelResponse(
            parts=[
                ThinkingPart("", signature="thought-sig", provider_name=PROVIDER),
                ToolCallPart(
                    "execute_analysis",
                    {"sql": "SELECT 1"},
                    tool_call_id="call_1",
                    provider_name=PROVIDER,
                    provider_details={"signature": "call-sig"},
                ),
            ],
            provider_name=PROVIDER,
        ),
        ModelRequest(
            parts=[ToolReturnPart("execute_analysis", {"evidence": "ev-1"}, "call_1")]
        ),
        ModelResponse(
            parts=[
                ThinkingPart("private gpt reasoning", provider_name="openai"),
                TextPart("Checking more."),
                ToolCallPart(
                    "execute_analysis",
                    {"sql": "SELECT 2"},
                    tool_call_id="call_gpt",
                    provider_name="openai",
                ),
            ],
            provider_name="openai",
        ),
        ModelRequest(
            parts=[
                RetryPromptPart(
                    "invalid", tool_name="execute_analysis", tool_call_id="call_gpt"
                )
            ]
        ),
    ]
    recorder = stubs.Recorder([stubs.Reply(events=stubs.gemini_text("Done."))])

    await stubs.gemini(recorder).request(history, None, PARAMS)

    steps = recorder.requests[0]["input"]
    assert [s["type"] for s in steps] == [
        "user_input",
        "thought",
        "function_call",
        "function_result",
        "model_output",
        "function_call",
        "function_result",
    ]
    assert steps[1]["signature"] == "thought-sig"
    assert steps[2]["signature"] == "call-sig"
    assert steps[3]["call_id"] == "call_1"
    assert json.loads(steps[3]["result"][0]["text"]) == {"evidence": "ev-1"}
    assert steps[5]["signature"] == FOREIGN_CALL_SIGNATURE
    assert steps[6]["is_error"] is True
    assert "private gpt reasoning" not in json.dumps(recorder.requests[0])


async def test_throttling_is_a_sanitized_http_error_with_retry_hint() -> None:
    reply = stubs.error(
        429,
        "resource_exhausted",
        details=[{"@type": "RetryInfo", "retryDelay": "17s"}],
    )
    recorder = stubs.Recorder([reply])
    with pytest.raises(ModelHTTPError) as raised:
        await stubs.gemini(recorder).request(QUESTION, None, PARAMS)
    assert raised.value.status_code == 429
    assert raised.value.headers == {"retry-after": "17"}
    assert raised.value.body == {"code": "resource_exhausted", "message": "x"}
    assert stubs.GEMINI_KEY not in str(raised.value)


async def test_stream_error_event_maps_to_a_status() -> None:
    events: list[dict[str, Any]] = [
        {"event_type": "step.start", "index": 0, "step": {"type": "model_output"}},
        {
            "event_type": "error",
            "error": {"code": "gateway_timeout", "message": "Deadline expired"},
        },
    ]
    recorder = stubs.Recorder([stubs.Reply(events=events)])
    with pytest.raises(ModelHTTPError) as raised:
        await stubs.gemini(recorder).request(QUESTION, None, PARAMS)
    assert raised.value.status_code == 504


async def test_failed_interaction_is_an_api_error() -> None:
    events = [
        {
            "event_type": "interaction.completed",
            "interaction": {"id": "x", "status": "failed"},
        }
    ]
    recorder = stubs.Recorder([stubs.Reply(events=events)])
    with pytest.raises(ModelAPIError, match="failed"):
        await stubs.gemini(recorder).request(QUESTION, None, PARAMS)


async def test_cut_stream_reports_no_usage() -> None:
    events = stubs.gemini_text("Partial")[:2]
    recorder = stubs.Recorder([stubs.Reply(events=events)])
    response = await stubs.gemini(recorder).request(QUESTION, None, PARAMS)
    assert response.usage.input_tokens == 0
