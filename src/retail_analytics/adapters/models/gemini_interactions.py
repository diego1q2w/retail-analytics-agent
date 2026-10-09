"""Gemini through the Interactions API, as a Pydantic AI model.

Pydantic AI's native Google model calls ``generateContent``; the selected
primary (``gemini-3.8-flash``) is served only through the Interactions API
(``POST /v1beta/interactions``). This adapter is deliberately narrow: text
prompts, function tools (the permission-filtered catalog plus output tools),
text and function-call output, streamed over server-sent events.

Every request is stateless (``store: false``): the application-owned message
history is sent in full, nothing is kept on the provider for later turns.
Gemini's own reasoning is never exposed: only its opaque signatures are echoed
back, as the API requires for function calls. Calls made by another provider
(after a fallback) carry the documented placeholder signature instead, and
reasoning recorded by another provider is never forwarded.

The API key travels in a header, never in the URL, and error details keep
only the provider's error code and message.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic_ai import _utils
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UserError
from pydantic_ai.messages import (
    FinishReason,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import (
    Model,
    ModelRequestParameters,
    StreamedResponse,
    check_allow_model_requests,
)
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.profiles.google import google_model_profile
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RequestUsage

PROVIDER = "google-interactions"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Documented placeholder for function calls the model did not produce itself.
FOREIGN_CALL_SIGNATURE = "skip_thought_signature_validator"
_SIGNATURE = "signature"
_MAX_ERROR_TEXT = 300
_DELAY = re.compile(r"(\d+(?:\.\d+)?)s")

# Stream error codes that correspond to an HTTP status (for retry decisions).
_ERROR_STATUS = {
    "invalid_request": 400,
    "invalid_argument": 400,
    "permission_denied": 403,
    "not_found": 404,
    "resource_exhausted": 429,
    "rate_limit_exceeded": 429,
    "internal": 500,
    "unavailable": 503,
    "gateway_timeout": 504,
    "deadline_exceeded": 504,
}
_FINISH = {
    "completed": "stop",
    "requires_action": "tool_call",
    "incomplete": "length",
}


class GeminiInteractionsModel(Model):
    """Streams one stateless interaction per request."""

    def __init__(
        self,
        model_name: str,
        *,
        api_key: str,
        http_client: httpx.AsyncClient,
        base_url: str = DEFAULT_BASE_URL,
        max_output_tokens: int | None = None,
        settings: ModelSettings | None = None,
    ) -> None:
        profile = (google_model_profile(model_name) or ModelProfile()).copy()
        # Catalog tools carry no return schemas; keep definitions minimal.
        profile["supports_tool_return_schema"] = False
        super().__init__(settings=settings, profile=profile)
        self._model_name = model_name
        self._api_key = api_key
        self._client = http_client
        self._url = base_url.rstrip("/") + "/interactions"
        self._max_output_tokens = max_output_tokens

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def system(self) -> str:
        return PROVIDER

    @property
    def base_url(self) -> str:
        return self._url

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        async with self.request_stream(
            messages, model_settings, model_request_parameters
        ) as stream:
            async for _ in stream:
                pass
        return stream.get()

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        check_allow_model_requests()
        model_settings, model_request_parameters = self.prepare_request(
            model_settings, model_request_parameters
        )
        body = self.interaction_body(messages, model_settings, model_request_parameters)
        request = self._client.build_request(
            "POST",
            self._url,
            json=body,
            headers={
                "x-goog-api-key": self._api_key,
                "accept": "text/event-stream",
            },
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as error:
            raise ModelAPIError(self._model_name, _transport_error(error)) from None
        try:
            if response.status_code != 200:
                try:
                    await response.aread()
                except httpx.HTTPError as error:
                    raise ModelAPIError(
                        self._model_name, _transport_error(error)
                    ) from None
                raise http_error(self._model_name, response)
            yield InteractionStream(
                model_request_parameters=model_request_parameters,
                _model_name=self._model_name,
                _lines=response.aiter_lines(),
            )
        finally:
            # A broken connection may also fail to close; the request's own
            # outcome (answer or error) is what the caller must see.
            with suppress(httpx.HTTPError):
                await response.aclose()

    def interaction_body(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        parameters: ModelRequestParameters,
    ) -> dict[str, Any]:
        """The JSON request for ``messages`` (prepared parameters)."""
        system, steps = _steps(messages)
        for part in self._get_instruction_parts(messages, parameters) or []:
            system.append(part.content)
        tools = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description or "",
                "parameters": provider_schema(tool.parameters_json_schema),
            }
            for tool in [*parameters.function_tools, *parameters.output_tools]
        ]
        config: dict[str, Any] = {}
        if tools:
            config["tool_choice"] = "auto" if parameters.allow_text_output else "any"
        max_tokens = (model_settings or {}).get("max_tokens", self._max_output_tokens)
        if max_tokens is not None:
            config["max_output_tokens"] = max_tokens
        body: dict[str, Any] = {
            "model": self._model_name,
            "input": steps,
            "store": False,
            "stream": True,
        }
        if system:
            body["system_instruction"] = "\n\n".join(system)
        if tools:
            body["tools"] = tools
        if config:
            body["generation_config"] = config
        return body


# Array-length keywords the Interactions API cannot take in bulk: with
# ``tool_choice: any`` the full catalog's ``maxItems`` bounds make it reject the
# request ("invalid argument", HTTP 400). Arguments are still validated against
# the full schema when the tool call is parsed, so dropping them here only
# changes what the provider is told, never what the application accepts.
_UNSENT_KEYWORDS = frozenset({"maxItems"})
_NAMED_SUBSCHEMAS = frozenset({"properties", "$defs", "definitions"})


def provider_schema(schema: Any) -> Any:
    """``schema`` without the keywords the provider rejects (property names kept)."""
    if isinstance(schema, list):
        return [provider_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSENT_KEYWORDS:
            continue
        if key in _NAMED_SUBSCHEMAS and isinstance(value, dict):
            cleaned[key] = {name: provider_schema(sub) for name, sub in value.items()}
        else:
            cleaned[key] = provider_schema(value)
    return cleaned


def _steps(messages: list[ModelMessage]) -> tuple[list[str], list[dict[str, Any]]]:
    system: list[str] = []
    steps: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    system.append(part.content)
                elif isinstance(part, UserPromptPart):
                    steps.append(_user_input(_prompt_text(part)))
                elif isinstance(part, ToolReturnPart):
                    steps.append(
                        _function_result(
                            part.tool_call_id, part.tool_name, part.model_response_str()
                        )
                    )
                elif isinstance(part, RetryPromptPart):
                    if part.tool_name is None:
                        steps.append(_user_input(part.model_response()))
                    else:
                        steps.append(
                            _function_result(
                                part.tool_call_id,
                                part.tool_name,
                                part.model_response(),
                                is_error=True,
                            )
                        )
        else:
            steps.extend(_response_steps(message))
    return system, steps


def _response_steps(message: ModelResponse) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            if part.content:
                steps.append(
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": part.content}],
                    }
                )
        elif isinstance(part, ThinkingPart):
            # Only this provider's opaque signature; never reasoning text,
            # and never anything another provider produced.
            if part.provider_name == PROVIDER and part.signature:
                steps.append({"type": "thought", _SIGNATURE: part.signature})
        elif isinstance(part, ToolCallPart):
            own = (
                part.provider_name == PROVIDER
                and isinstance(part.provider_details, dict)
                and isinstance(part.provider_details.get(_SIGNATURE), str)
            )
            signature = (
                part.provider_details[_SIGNATURE]  # type: ignore[index]
                if own
                else FOREIGN_CALL_SIGNATURE
            )
            steps.append(
                {
                    "type": "function_call",
                    "id": part.tool_call_id,
                    "name": part.tool_name,
                    "arguments": part.args_as_dict(),
                    _SIGNATURE: signature,
                }
            )
    return steps


def _user_input(text: str) -> dict[str, Any]:
    return {"type": "user_input", "content": [{"type": "text", "text": text}]}


def _function_result(
    call_id: str, name: str, text: str, *, is_error: bool = False
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "type": "function_result",
        "call_id": call_id,
        "name": name,
        "result": [{"type": "text", "text": text}],
    }
    if is_error:
        step["is_error"] = True
    return step


def _prompt_text(part: UserPromptPart) -> str:
    if isinstance(part.content, str):
        return part.content
    texts = [item for item in part.content if isinstance(item, str)]
    if len(texts) != len(part.content):
        raise UserError("only text prompts are supported by this provider adapter")
    return "\n".join(texts)


@dataclass
class InteractionStream(StreamedResponse):
    """Turns the interaction's server-sent events into Pydantic AI events."""

    _model_name: str
    _lines: AsyncIterator[str]
    _timestamp: datetime = field(default_factory=_utils.now_utc)
    _calls: set[int] = field(default_factory=set)

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        # The connection can also fail while the body streams (after HTTP
        # 200): that is the same transient connection failure as one before
        # the response started, so retries and fallback apply to it too.
        try:
            async for data in _sse_data(self._lines):
                kind = data.get("event_type")
                if kind == "step.start":
                    event = self._start(data)
                    if event is not None:
                        yield event
                elif kind == "step.delta":
                    for event in self._delta(data):
                        yield event
                elif kind == "interaction.completed":
                    self._complete(data.get("interaction") or {})
                elif kind == "error":
                    raise _stream_error(self._model_name, data.get("error"))
        except httpx.HTTPError as error:
            raise ModelAPIError(self._model_name, _transport_error(error)) from None

    def _start(self, data: Mapping[str, Any]) -> ModelResponseStreamEvent | None:
        index = data.get("index")
        step = data.get("step") or {}
        if not isinstance(index, int) or step.get("type") != "function_call":
            return None
        self._calls.add(index)
        arguments = step.get("arguments")
        signature = step.get(_SIGNATURE)
        return self._parts_manager.handle_tool_call_delta(
            vendor_part_id=("call", index),
            tool_name=step.get("name"),
            args=json.dumps(arguments) if arguments else None,
            tool_call_id=step.get("id"),
            provider_name=PROVIDER,
            provider_details=(
                {_SIGNATURE: signature} if isinstance(signature, str) else None
            ),
        )

    def _delta(self, data: Mapping[str, Any]) -> list[ModelResponseStreamEvent]:
        index = data.get("index")
        delta = data.get("delta") or {}
        kind = delta.get("type")
        if kind == "text" and isinstance(delta.get("text"), str):
            return list(
                self._parts_manager.handle_text_delta(
                    vendor_part_id=("text", index),
                    content=delta["text"],
                    provider_name=PROVIDER,
                )
            )
        if kind == "arguments_delta" and isinstance(delta.get("arguments"), str):
            event = self._parts_manager.handle_tool_call_delta(
                vendor_part_id=("call", index), args=delta["arguments"]
            )
            return [] if event is None else [event]
        if kind == "thought_signature" and isinstance(delta.get(_SIGNATURE), str):
            if index in self._calls:
                # A signature for a function-call step travels with the call.
                event = self._parts_manager.handle_tool_call_delta(
                    vendor_part_id=("call", index),
                    provider_details={_SIGNATURE: delta[_SIGNATURE]},
                )
                return [] if event is None else [event]
            return list(
                self._parts_manager.handle_thinking_delta(
                    vendor_part_id=("thought", index),
                    signature=delta[_SIGNATURE],
                    provider_name=PROVIDER,
                )
            )
        # Thought summaries are not requested; other step kinds are not used.
        return []

    def _complete(self, interaction: Mapping[str, Any]) -> None:
        status = interaction.get("status")
        if status in ("failed", "cancelled"):
            raise ModelAPIError(self._model_name, f"interaction {status}")
        self.provider_response_id = interaction.get("id") or None
        self.finish_reason = _finish_reason(status)
        usage = interaction.get("usage")
        if isinstance(usage, Mapping):
            self._usage = interaction_usage(usage)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str:
        return PROVIDER

    @property
    def provider_url(self) -> str:
        return DEFAULT_BASE_URL

    @property
    def timestamp(self) -> datetime:
        return self._timestamp


def interaction_usage(usage: Mapping[str, Any]) -> RequestUsage:
    """Billed tokens in Pydantic AI's normalized form.

    Interactions API usage (checked against the API reference and pricing
    page, 2026-10): ``total_input_tokens`` is the whole prompt, the cached
    part (``total_cached_tokens``) included; ``total_output_tokens`` excludes
    thinking, reported separately as ``total_thought_tokens`` and billed at
    the output price; ``total_tokens`` is input + output + thoughts.
    ``total_tool_use_tokens`` (server-side tool prompts) are not part of
    ``total_tokens``; this agent uses no server-side tools, so it is zero in
    practice, and it is counted as input (the conservative choice). Thoughts
    and tool-use tokens are added once here; ``details`` keeps the raw
    subsets for audit only.
    """

    def count(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and value >= 0 else 0

    thoughts = count("total_thought_tokens")
    tool_use = count("total_tool_use_tokens")
    return RequestUsage(
        input_tokens=count("total_input_tokens") + tool_use,
        output_tokens=count("total_output_tokens") + thoughts,
        cache_read_tokens=count("total_cached_tokens"),
        details={"reasoning_tokens": thoughts, "tool_use_tokens": tool_use},
    )


def _finish_reason(status: object) -> FinishReason | None:
    reason = _FINISH.get(status) if isinstance(status, str) else None
    return reason  # type: ignore[return-value]


async def _sse_data(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """``data:`` payloads of a server-sent event stream, decoded as JSON."""
    async for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if isinstance(data, dict):
            yield data


def http_error(model_name: str, response: httpx.Response) -> ModelHTTPError:
    """A sanitized provider error: status, error code/message, retry hint."""
    body: dict[str, Any] = {}
    retry_after: str | None = response.headers.get("retry-after")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        body = _error_summary(error)
        delay = _retry_delay(error)
        if delay is not None and retry_after is None:
            retry_after = delay
    headers = {"retry-after": retry_after} if retry_after else None
    return ModelHTTPError(response.status_code, model_name, body, headers=headers)


def _stream_error(model_name: str, error: object) -> ModelAPIError:
    if not isinstance(error, dict):
        return ModelAPIError(model_name, "interaction stream error")
    summary = _error_summary(error)
    code = summary.get("code")
    status = _ERROR_STATUS.get(code) if isinstance(code, str) else None
    if isinstance(error.get("code"), int):
        status = error["code"]
    if status is None:
        return ModelAPIError(model_name, f"interaction stream error: {code}")
    return ModelHTTPError(status, model_name, summary)


def _error_summary(error: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("code", "status", "message"):
        value = error.get(key)
        if isinstance(value, str | int):
            summary[key] = value[:_MAX_ERROR_TEXT] if isinstance(value, str) else value
    return summary


def _retry_delay(error: Mapping[str, Any]) -> str | None:
    """Seconds from a google.rpc.RetryInfo ``retryDelay`` such as ``"17s"``."""
    details = error.get("details")
    if not isinstance(details, list):
        return None
    for detail in details:
        delay = detail.get("retryDelay") if isinstance(detail, dict) else None
        match = _DELAY.fullmatch(delay) if isinstance(delay, str) else None
        if match:
            return match.group(1)
    return None


def _transport_error(error: httpx.HTTPError) -> str:
    # The class name only: transport messages can embed request details.
    return f"provider connection failed ({type(error).__name__})"
