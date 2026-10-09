"""FastAPI application factory. Routes stay thin and call application use cases.

Bootstrap passes a ``services`` provider: an async context manager entered at
start-up (connections, Temporal client) whose ``HttpServices`` serve every
``/v1`` request. Without it only ``/healthz`` answers; ``/v1`` routes return
503 ``unavailable``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from retail_analytics.interfaces.http.errors import install_error_handlers
from retail_analytics.interfaces.http.routes import build_router
from retail_analytics.interfaces.http.schemas import API_VERSION
from retail_analytics.interfaces.http.services import ServicesProvider, StreamSettings


class HealthResponse(BaseModel):
    status: str
    mode: str
    version: str


def create_app(
    *,
    mode: str,
    version: str,
    services: ServicesProvider | None = None,
    stream: StreamSettings | None = None,
) -> FastAPI:
    """Build the HTTP app. Bootstrap supplies everything it needs explicitly."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if services is None:
            yield
            return
        async with services() as composed:
            app.state.services = composed
            try:
                yield
            finally:
                app.state.services = None

    app = FastAPI(
        title="Retail Analytics Agent",
        version=version,
        summary=f"Authenticated investigation API v{API_VERSION} (bearer tokens).",
        lifespan=lifespan,
    )
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok", mode=mode, version=version)

    app.include_router(build_router(stream or StreamSettings()))
    return app
