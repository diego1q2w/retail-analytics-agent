"""The provider-boundary view of one model attempt, for trace capture.

Builds explicit, JSON-like structures from the Pydantic AI messages actually
handed to a provider model and from its assembled response, in the widely
used chat shape (``messages`` with ``role``/``content``/``tool_calls``;
``choices``) so trace viewers can render a conversation. Built here because
only this adapter knows the Pydantic AI message types; sanitizing and
bounding happen in the application (``SpanRecorder.inputs``/``outputs``).

Never included: reasoning text (``ThinkingPart``), provider details such as
thought signatures or opaque replay ids, binary or file content and provider
built-in tool payloads. Each is counted under ``omitted`` instead (reasoning as
``provider_private_parts``), so the trace says what it does not show.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError
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
from pydantic_core import to_jsonable_python

MAX_ERROR_CHARS = 500


def request_payload(
    messages: Sequence[ModelMessage],
    parameters: ModelRequestParameters,
    *,
    provider: str,
    model: str,
    attempt: int,
) -> dict[str, object]:
    omitted: Counter[str] = Counter()
    chat: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            instructions = getattr(message, "instructions", None)
            if isinstance(instructions, str) and instructions:
                chat.append({"role": "system", "content": instructions})
            for part in message.parts:
                chat.extend(_request_part(part, omitted))
        else:
            chat.append(_assistant(message.parts, omitted))
    payload: dict[str, object] = {
        "provider": provider,
        "model": model,
        "attempt": attempt,
        "messages": chat,
        "tools": sorted(tool.name for tool in parameters.function_tools),
        "output_tools": sorted(tool.name for tool in parameters.output_tools),
    }
    if omitted:
        payload["omitted"] = dict(sorted(omitted.items()))
    return payload


def response_payload(response: ModelResponse) -> dict[str, object]:
    omitted: Counter[str] = Counter()
    message = _assistant(response.parts, omitted)
    payload: dict[str, object] = {
        "model": response.model_name,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": response.finish_reason,
            }
        ],
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }
    if omitted:
        payload["omitted"] = dict(sorted(omitted.items()))
    return payload


def error_payload(error: BaseException, reason_class: str) -> dict[str, object]:
    """A failed attempt: class, status and the (sanitized later) message."""
    detail: dict[str, object] = {
        "type": type(error).__name__,
        "reason_class": reason_class,
        "message": str(error)[:MAX_ERROR_CHARS],
    }
    if isinstance(error, ModelHTTPError):
        detail["status_code"] = error.status_code
    return {"error": detail}


def _request_part(part: object, omitted: Counter[str]) -> list[dict[str, object]]:
    if isinstance(part, SystemPromptPart):
        return [{"role": "system", "content": part.content}]
    if isinstance(part, UserPromptPart):
        if isinstance(part.content, str):
            return [{"role": "user", "content": part.content}]
        texts = [item for item in part.content if isinstance(item, str)]
        if len(texts) != len(part.content):
            omitted["non_text_user_content"] += len(part.content) - len(texts)
        return [{"role": "user", "content": "\n".join(texts)}]
    if isinstance(part, ToolReturnPart):
        return [
            {
                "role": "tool",
                "tool_call_id": part.tool_call_id,
                "name": part.tool_name,
                "content": _jsonable(part.content, omitted),
            }
        ]
    if isinstance(part, RetryPromptPart):
        return [
            {
                "role": "tool" if part.tool_name else "user",
                "tool_call_id": part.tool_call_id,
                "name": part.tool_name,
                "content": part.model_response(),
            }
        ]
    omitted[f"request_part:{type(part).__name__}"] += 1
    return []


def _assistant(parts: Sequence[object], omitted: Counter[str]) -> dict[str, object]:
    texts: list[str] = []
    calls: list[dict[str, object]] = []
    for part in parts:
        if isinstance(part, TextPart):
            texts.append(part.content)
        elif isinstance(part, ToolCallPart):
            # Only name, id and arguments: provider_details carries opaque
            # signatures that must never leave the provider adapter.
            calls.append(
                {
                    "id": part.tool_call_id,
                    "type": "function",
                    "function": {"name": part.tool_name, "arguments": _args(part)},
                }
            )
        elif isinstance(part, ThinkingPart):
            omitted["provider_private_parts"] += 1
        else:
            omitted[f"response_part:{type(part).__name__}"] += 1
    message: dict[str, object] = {"role": "assistant", "content": "\n".join(texts)}
    if calls:
        message["tool_calls"] = calls
    return message


def _args(part: ToolCallPart) -> object:
    try:
        return part.args_as_dict()
    except Exception:
        return "[omitted: arguments are not valid JSON]"


def _jsonable(value: Any, omitted: Counter[str]) -> object:
    try:
        return to_jsonable_python(value, bytes_mode="base64")
    except Exception:
        omitted["unserializable_tool_result"] += 1
        return "[omitted: unserializable tool result]"
