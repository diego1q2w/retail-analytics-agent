"""Request spans and metrics for the HTTP API (pure ASGI middleware).

Records the route template (never the raw path), method and status class.
Path identifiers (run, session) go on the span only, never on metrics; a
request about a run carries that run's trace id so the span can be followed to
the run's trace. Request bodies, query strings and headers are not read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.telemetry import Stopwatch, telemetry, trace_id_for

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type App = Callable[[Scope, Receive, Send], Awaitable[None]]

_UNMATCHED = "unmatched"


class TelemetryMiddleware:
    def __init__(self, app: App) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        status = {"code": 500}

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        method = str(scope.get("method", "GET"))
        watch = Stopwatch()
        with telemetry().span(Span.HTTP, attributes={"method": method}) as span:
            try:
                await self._app(scope, receive, observed_send)
            finally:
                route = getattr(scope.get("route"), "path", None)
                template = route if isinstance(route, str) else _UNMATCHED
                params = scope.get("path_params") or {}
                attributes: dict[str, object] = {
                    # Braces would read as a data dump; /v1/runs/:run_id instead.
                    "route": template.replace("{", ":").replace("}", ""),
                    "status_code": status["code"],
                }
                for key in ("run_id", "session_id"):
                    if isinstance(params.get(key), str):
                        attributes[key] = params[key]
                if isinstance(params.get("run_id"), str):
                    attributes["run_trace_id"] = trace_id_for(params["run_id"])
                span.set(attributes)
                if status["code"] >= 500:
                    span.fail(f"http_{status['code']}")
                telemetry().count(
                    Metric.HTTP_REQUESTS,
                    {
                        Label.ROUTE: template,
                        Label.METHOD: method,
                        Label.STATUS: f"{status['code'] // 100}xx",
                    },
                )
                telemetry().observe(
                    Metric.HTTP_SECONDS, watch.seconds(), {Label.ROUTE: template}
                )
