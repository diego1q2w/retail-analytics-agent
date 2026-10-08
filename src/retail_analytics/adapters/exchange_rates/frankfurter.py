"""Exchange rates from the public Frankfurter API, pinned to the ECB.

Frankfurter (https://frankfurter.dev, v2) needs no API key and has no quota,
only abuse rate limiting. Without a ``providers`` filter it blends 100+ central
banks, so a day's rate may come from a different bank than yesterday's. This
adapter always asks for ``providers=ECB`` so one documented source stands
behind every figure: the European Central Bank euro foreign exchange reference
rates, published on TARGET business days (about 16:00 CET), mid-market, with
non-euro pairs crossed through the euro. ECB covers roughly 30 currencies; a
pair outside that set has no rate here, and the service reports it unavailable.

Date semantics: ``GET /rates?base=B&quotes=Q[&date=D]`` returns a JSON list with
the effective ``date`` of each rate, which is the latest published business day
on or before the requested one. An empty list means no rate (unsupported
currency, or a date outside the ECB's history). That effective date is what the
caller discloses.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from retail_analytics.application.currency_conversion import (
    RateProviderFailure,
    RateUnavailable,
)
from retail_analytics.domain.exchange_rates import ExchangeRate, InvalidRate

DEFAULT_BASE_URL = "https://api.frankfurter.dev/v2"
SOURCE = "European Central Bank reference rates via Frankfurter"
METHOD = "daily mid-market reference rate; non-euro pairs crossed through EUR"
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_DELAY = 10.0


class FrankfurterRateProvider:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        base_delay: float = 0.5,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._url = base_url.rstrip("/") + "/rates"
        self._max_attempts = max_attempts
        self._base_delay = base_delay

    async def rate(self, base: str, quote: str, *, on: date | None) -> ExchangeRate:
        params = {"base": base, "quotes": quote, "providers": "ECB"}
        if on is not None:
            params["date"] = on.isoformat()
        payload = await self._get(params)
        return _parse(payload, base, quote, on)

    async def _get(self, params: dict[str, str]) -> object:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(self._url, params=params)
            except httpx.HTTPError:
                if attempt == self._max_attempts:
                    raise RateProviderFailure("transport error") from None
                await asyncio.sleep(self._base_delay * 2 ** (attempt - 1))
                continue
            status = response.status_code
            if status == 422:
                # An unknown currency code or malformed date: a definite answer.
                raise RateUnavailable("provider rejected the pair or date")
            if status in _RETRYABLE and attempt < self._max_attempts:
                await asyncio.sleep(self._delay(response, attempt))
                continue
            if status != 200:
                raise RateProviderFailure(f"provider answered {status}")
            try:
                return response.json()
            except ValueError:
                raise RateProviderFailure("provider returned invalid JSON") from None
        raise AssertionError("unreachable")

    def _delay(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("retry-after", "")
        if header.isdigit():
            return min(float(header), _MAX_RETRY_DELAY)
        return float(self._base_delay * 2 ** (attempt - 1))

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse(payload: object, base: str, quote: str, on: date | None) -> ExchangeRate:
    if not isinstance(payload, list):
        raise RateProviderFailure("unexpected response shape")
    if not payload:
        raise RateUnavailable("no published rate")
    matches = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("base") == base
        and item.get("quote") == quote
    ]
    if len(matches) != 1:
        raise RateProviderFailure("unexpected response contents")
    item = matches[0]
    try:
        raw = item["rate"]
        if isinstance(raw, bool) or not isinstance(raw, int | float | str):
            raise TypeError
        return ExchangeRate(
            base,
            quote,
            Decimal(str(raw)),
            date.fromisoformat(str(item["date"])),
            SOURCE,
            METHOD,
            requested_date=on,
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, InvalidRate):
        raise RateProviderFailure("malformed rate in response") from None
