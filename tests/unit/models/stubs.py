"""HTTP stubs for the real provider adapters (no network).

``GeminiStub`` and ``OpenAIStub`` answer the adapters' streaming requests
from scripted replies and record every request body they receive, so tests
exercise the actual request mapping, streaming parsers and error handling.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import httpx2
from openai import DefaultAsyncHttpxClient
from pydantic import SecretStr
from pydantic_ai.models.openai import OpenAIResponsesModel

from retail_analytics.adapters.models.gemini_interactions import (
    GeminiInteractionsModel,
)
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.models import gemini_model, openai_model

GEMINI_KEY = "gemini-test-key-0123456789"
OPENAI_KEY = "sk-openai-test-key-0123456789"
NOW = datetime(2026, 10, 9, tzinfo=UTC)


def sse(events: list[dict[str, Any]], *, done: bool = True) -> bytes:
    lines = []
    for event in events:
        name = event.get("event_type") or event.get("type")
        lines.append(f"event: {name}\ndata: {json.dumps(event)}\n\n")
    if done:
        lines.append("event: done\ndata: [DONE]\n\n")
    return "".join(lines).encode()


# Gemini Interactions replies


def gemini_usage(input_tokens: int = 100, output_tokens: int = 20) -> dict[str, Any]:
    return {
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "total_thought_tokens": 5,
        "total_tool_use_tokens": 0,
        "total_tokens": input_tokens + output_tokens + 5,
    }


def gemini_call(
    name: str, arguments: dict[str, Any], *, call_id: str = "call_1"
) -> list[dict[str, Any]]:
    return [
        {"event_type": "interaction.created", "interaction": {"model": "g"}},
        {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
        {
            "event_type": "step.delta",
            "index": 0,
            "delta": {"type": "thought_signature", "signature": "thought-sig"},
        },
        {"event_type": "step.stop", "index": 0},
        {
            "event_type": "step.start",
            "index": 1,
            "step": {
                "type": "function_call",
                "id": call_id,
                "name": name,
                "arguments": {},
                "signature": "call-sig",
            },
        },
        {
            "event_type": "step.delta",
            "index": 1,
            "delta": {"type": "arguments_delta", "arguments": json.dumps(arguments)},
        },
        {"event_type": "step.stop", "index": 1},
        {
            "event_type": "interaction.completed",
            "interaction": {
                "id": "int-1",
                "status": "requires_action",
                "usage": gemini_usage(),
            },
        },
    ]


def gemini_text(text: str) -> list[dict[str, Any]]:
    return [
        {"event_type": "step.start", "index": 0, "step": {"type": "model_output"}},
        {
            "event_type": "step.delta",
            "index": 0,
            "delta": {"type": "text", "text": text},
        },
        {"event_type": "step.stop", "index": 0},
        {
            "event_type": "interaction.completed",
            "interaction": {
                "id": "int-2",
                "status": "completed",
                "usage": gemini_usage(),
            },
        },
    ]


@dataclass
class Reply:
    status: int = 200
    events: list[dict[str, Any]] = field(default_factory=list)
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


def error(status: int, code: str, **extra: Any) -> Reply:
    return Reply(status, body={"error": {"code": code, "message": "x", **extra}})


@dataclass
class Recorder:
    replies: list[Reply]
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    # Optional: compute each reply from the request body instead.
    script: Callable[[dict[str, Any]], Reply] | None = None

    def answer(self, url: str, headers: dict[str, str], body: bytes) -> Reply:
        self.urls.append(url)
        self.headers.append(headers)
        request = json.loads(body)
        self.requests.append(request)
        if self.script is not None:
            return self.script(request)
        if not self.replies:
            raise AssertionError("unexpected provider request")
        return self.replies.pop(0)


def _respond(reply: Reply, factory: Callable[..., Any]) -> Any:
    if reply.status != 200:
        return factory(reply.status, json=reply.body, headers=reply.headers)
    return factory(
        200,
        content=sse(reply.events),
        headers={"content-type": "text/event-stream"},
    )


def gemini(
    recorder: Recorder, model_name: str = "gemini-3.8-flash"
) -> GeminiInteractionsModel:
    def handle(request: httpx.Request) -> httpx.Response:
        reply = recorder.answer(
            str(request.url), dict(request.headers), request.content
        )
        response: httpx.Response = _respond(reply, httpx.Response)
        return response

    settings = BackendSettings(
        gemini_api_key=SecretStr(GEMINI_KEY), agent_gemini_model=model_name
    )
    return gemini_model(
        settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )


# OpenAI Responses replies


def _response(
    status: str, output: list[Any], usage: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1_760_000_000,
        "model": "gpt-5-mini",
        "status": status,
        "output": output,
        "usage": usage,
        "error": None,
        "incomplete_details": None,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def openai_call(
    name: str, arguments: dict[str, Any], *, tokens: int = 50
) -> list[dict[str, Any]]:
    item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_gpt_1",
        "name": name,
        "arguments": "",
        "status": "in_progress",
    }
    usage = {
        "input_tokens": tokens,
        "output_tokens": 10,
        "total_tokens": tokens + 10,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": _response("in_progress", [], None),
        },
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": item,
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 2,
            "output_index": 0,
            "item_id": "fc_1",
            "delta": json.dumps(arguments),
        },
        {
            "type": "response.completed",
            "sequence_number": 3,
            "response": _response("completed", [], usage),
        },
    ]


def openai(recorder: Recorder) -> OpenAIResponsesModel:
    def handle(request: httpx2.Request) -> httpx2.Response:
        reply = recorder.answer(
            str(request.url), dict(request.headers), request.content
        )
        response: httpx2.Response = _respond(reply, httpx2.Response)
        return response

    model = openai_model(
        BackendSettings(openai_api_key=SecretStr(OPENAI_KEY)),
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle)),
    )
    assert model is not None
    return model
