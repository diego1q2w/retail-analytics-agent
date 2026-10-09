"""Response-start, streaming-stall and total limits for one provider request.

Every request is streamed, even when the caller wants a whole response, so
the time to the first token can be told apart from a long answer that keeps
arriving:

- no first stream event within ``first_token`` seconds: the request fails;
- after that, a gap longer than ``stall`` seconds between events fails it;
- the whole request is bounded by ``total`` seconds.

A response that keeps progressing is therefore not cut off merely because it
runs past the first-token limit. Any streamed event counts as progress
(text, tool-call arguments or a reasoning signature). A timed-out request may
still have been processed by the provider: its usage is unknown, so the
budget keeps the reservation estimate (see ``BudgetedModel``).

The timeouts raise ``ModelResponseTimeout``, a ``ModelAPIError``, so the
fallback chain treats them like an unavailable provider.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse, ModelResponseStreamEvent
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

type WaitFor = Callable[[Awaitable[Any], float], Awaitable[Any]]


class TimeoutKind(StrEnum):
    FIRST_TOKEN = "first_token"  # noqa: S105 - a timeout name, not a secret
    STALL = "stream_stall"
    TOTAL = "total"


class ModelResponseTimeout(ModelAPIError):
    def __init__(self, model_name: str, kind: TimeoutKind, seconds: float) -> None:
        self.kind = kind
        self.seconds = seconds
        super().__init__(model_name, f"{kind.value} timeout after {seconds:g}s")

    def __reduce__(self) -> tuple[type, tuple[Any, ...]]:
        return self.__class__, (self.model_name, self.kind, self.seconds)


@dataclass(frozen=True)
class ResponseLimits:
    first_token_seconds: float = 60.0
    stall_seconds: float = 30.0
    total_seconds: float = 180.0

    def __post_init__(self) -> None:
        if min(self.first_token_seconds, self.stall_seconds) <= 0:
            raise ValueError("limits must be positive")
        if self.total_seconds < self.first_token_seconds:
            raise ValueError("total must not be shorter than the first-token limit")


async def _wait_for(awaitable: Awaitable[Any], seconds: float) -> Any:
    async with asyncio.timeout(seconds):
        return await awaitable


class StreamDeadlines(WrapperModel):
    def __init__(
        self,
        wrapped: Any,
        limits: ResponseLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait_for: WaitFor = _wait_for,
    ) -> None:
        super().__init__(wrapped)
        self._limits = limits
        self._clock = clock
        self._wait_for = wait_for

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        limits = self._limits
        started = self._clock()
        async with AsyncExitStack() as stack:

            async def first() -> tuple[StreamedResponse, AsyncIterator[Any], bool]:
                stream = await stack.enter_async_context(
                    self.wrapped.request_stream(
                        messages, model_settings, model_request_parameters
                    )
                )
                events = aiter(stream)
                return stream, events, await _advance(events)

            stream, events, more = await self._within(
                first(), limits.first_token_seconds, TimeoutKind.FIRST_TOKEN, started
            )
            while more:
                more = await self._within(
                    _advance(events), limits.stall_seconds, TimeoutKind.STALL, started
                )
            return stream.get()

    async def _within[T](
        self, step: Awaitable[T], limit: float, kind: TimeoutKind, started: float
    ) -> T:
        remaining = self._limits.total_seconds - (self._clock() - started)
        if remaining <= 0:
            _close(step)
            raise ModelResponseTimeout(
                self.model_name, TimeoutKind.TOTAL, self._limits.total_seconds
            )
        if remaining < limit:
            kind, limit = TimeoutKind.TOTAL, remaining
        try:
            result: T = await self._wait_for(step, limit)
        except TimeoutError:
            seconds = self._limits.total_seconds if kind is TimeoutKind.TOTAL else limit
            raise ModelResponseTimeout(self.model_name, kind, seconds) from None
        return result


async def _advance(events: AsyncIterator[ModelResponseStreamEvent]) -> bool:
    """Wait for the next stream event; False when the stream ended."""
    try:
        await anext(events)
    except StopAsyncIteration:
        return False
    return True


def _close(step: Awaitable[Any]) -> None:
    close = getattr(step, "close", None)
    if callable(close):
        close()
