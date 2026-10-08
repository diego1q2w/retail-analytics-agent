"""The Frankfurter adapter against faked HTTP: parsing, date semantics, faults."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from retail_analytics.adapters.exchange_rates.frankfurter import (
    METHOD,
    SOURCE,
    FrankfurterRateProvider,
)
from retail_analytics.application.currency_conversion import (
    RateProviderFailure,
    RateUnavailable,
)

pytestmark = pytest.mark.asyncio

OK = [{"date": "2026-10-02", "base": "USD", "quote": "EUR", "rate": 0.89087}]


def provider(
    *, responses: list[httpx.Response]
) -> tuple[FrankfurterRateProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return FrankfurterRateProvider(client=client, base_delay=0.0), seen


async def test_dated_request_pins_ecb_and_discloses_effective_date() -> None:
    p, seen = provider(responses=[httpx.Response(200, json=OK)])
    got = await p.rate("USD", "EUR", on=date(2026, 10, 4))
    assert dict(seen[0].url.params) == {
        "base": "USD",
        "quotes": "EUR",
        "providers": "ECB",
        "date": "2026-10-04",
    }
    assert got.rate == Decimal("0.89087")
    assert got.effective_date == date(2026, 10, 2)
    assert got.requested_date == date(2026, 10, 4)
    assert (got.source, got.method) == (SOURCE, METHOD)


async def test_latest_request_has_no_date_parameter() -> None:
    p, seen = provider(responses=[httpx.Response(200, json=OK)])
    got = await p.rate("USD", "EUR", on=None)
    assert "date" not in seen[0].url.params
    assert got.requested_date is None


async def test_empty_list_and_422_mean_no_rate() -> None:
    for response in (httpx.Response(200, json=[]), httpx.Response(422, json={})):
        p, _ = provider(responses=[response])
        with pytest.raises(RateUnavailable):
            await p.rate("USD", "ARS", on=None)


async def test_throttling_and_server_errors_are_retried_then_succeed() -> None:
    p, seen = provider(
        responses=[
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(503),
            httpx.Response(200, json=OK),
        ]
    )
    assert (await p.rate("USD", "EUR", on=None)).rate == Decimal("0.89087")
    assert len(seen) == 3


async def test_persistent_faults_fail_without_a_rate() -> None:
    p, seen = provider(responses=[httpx.Response(500)])
    with pytest.raises(RateProviderFailure):
        await p.rate("USD", "EUR", on=None)
    assert len(seen) == 3


async def test_transport_errors_are_provider_failures() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    p = FrankfurterRateProvider(client=client, base_delay=0.0)
    with pytest.raises(RateProviderFailure):
        await p.rate("USD", "EUR", on=None)


@pytest.mark.parametrize(
    "body",
    [
        {"rate": 1},
        [{"date": "2026-10-02", "base": "USD", "quote": "GBP", "rate": 0.8}],
        [{"date": "nope", "base": "USD", "quote": "EUR", "rate": 0.8}],
        [{"date": "2026-10-02", "base": "USD", "quote": "EUR", "rate": -1}],
        [{"date": "2026-10-02", "base": "USD", "quote": "EUR", "rate": "x"}],
        [{"date": "2026-10-02", "base": "USD", "quote": "EUR", "rate": None}],
        [{"date": "2026-10-05", "base": "USD", "quote": "EUR", "rate": 0.8}],
    ],
)
async def test_malformed_or_inconsistent_responses_are_rejected(body: object) -> None:
    p, _ = provider(responses=[httpx.Response(200, json=body)])
    with pytest.raises(RateProviderFailure):
        await p.rate("USD", "EUR", on=date(2026, 10, 4))
