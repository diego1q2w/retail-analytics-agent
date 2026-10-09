"""First-token, streaming-stall and total limits on a fake clock."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel

from retail_analytics.adapters.models.deadlines import (
    ModelResponseTimeout,
    ResponseLimits,
    StreamDeadlines,
    TimeoutKind,
)

pytestmark = pytest.mark.asyncio
MESSAGES: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Analyze.")])]
LIMITS = ResponseLimits(first_token_seconds=60, stall_seconds=30, total_seconds=180)


class _Expired(Exception):
    pass


class FakeClock:
    """Virtual time: a sleep past the active deadline expires that wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.deadline: float | None = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if self.deadline is not None and self.now + seconds > self.deadline:
            self.now = self.deadline
            raise _Expired
        self.now += seconds

    async def wait_for(self, awaitable: Awaitable[Any], seconds: float) -> Any:
        previous, self.deadline = self.deadline, self.now + seconds
        try:
            return await awaitable
        except _Expired:
            raise TimeoutError from None
        finally:
            self.deadline = previous


def streaming(
    clock: FakeClock, delays: list[float], *, opening: float = 0.0
) -> StreamDeadlines:
    """A provider that waits ``opening`` before the response, then streams
    one text chunk after each delay."""

    async def stream(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        for delay in delays:
            await clock.sleep(delay)
            yield "chunk "

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> Any:
        raise AssertionError("requests are always streamed")

    model = FunctionModel(respond, stream_function=stream)
    original = model.request_stream

    def opened(*args: Any, **kwargs: Any) -> Any:
        manager = original(*args, **kwargs)

        class Slow:
            async def __aenter__(self) -> Any:
                await clock.sleep(opening)
                return await manager.__aenter__()

            async def __aexit__(self, *exc: Any) -> Any:
                return await manager.__aexit__(*exc)

        return Slow()

    model.request_stream = opened  # type: ignore[method-assign]
    return StreamDeadlines(model, LIMITS, clock=clock, wait_for=clock.wait_for)


async def _request(model: StreamDeadlines) -> Any:
    return await model.request(MESSAGES, None, ModelRequestParameters())


async def test_no_first_token_within_sixty_seconds_times_out() -> None:
    clock = FakeClock()
    with pytest.raises(ModelResponseTimeout) as raised:
        await _request(streaming(clock, [61.0]))
    assert raised.value.kind is TimeoutKind.FIRST_TOKEN
    assert clock.now == 60.0


async def test_slow_response_start_counts_toward_the_first_token() -> None:
    clock = FakeClock()
    with pytest.raises(ModelResponseTimeout) as raised:
        await _request(streaming(clock, [20.0], opening=45.0))
    assert raised.value.kind is TimeoutKind.FIRST_TOKEN


async def test_progressing_stream_is_not_cut_off_at_sixty_seconds() -> None:
    clock = FakeClock()
    # First token at 59 s, then a chunk every 25 s until 159 s.
    response = await _request(streaming(clock, [59.0, 25.0, 25.0, 25.0, 25.0]))
    assert clock.now == 159.0
    assert "chunk" in response.parts[0].content


async def test_stall_after_the_first_token_times_out() -> None:
    clock = FakeClock()
    with pytest.raises(ModelResponseTimeout) as raised:
        await _request(streaming(clock, [5.0, 10.0, 31.0]))
    assert raised.value.kind is TimeoutKind.STALL
    assert clock.now == 45.0


async def test_total_limit_bounds_an_endless_stream() -> None:
    clock = FakeClock()
    with pytest.raises(ModelResponseTimeout) as raised:
        await _request(streaming(clock, [10.0] + [20.0] * 20))
    assert raised.value.kind is TimeoutKind.TOTAL
    assert clock.now == 180.0


async def test_limits_reject_a_total_shorter_than_the_first_token() -> None:
    with pytest.raises(ValueError):
        ResponseLimits(first_token_seconds=60, stall_seconds=30, total_seconds=59)
