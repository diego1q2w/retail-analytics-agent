"""FastAPI application factory. Routes stay thin and call application use cases."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    mode: str
    version: str


def create_app(*, mode: str, version: str) -> FastAPI:
    """Build the HTTP app. Bootstrap supplies everything it needs explicitly."""
    app = FastAPI(title="Retail Analytics Agent", version=version)

    @app.get("/healthz")
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok", mode=mode, version=version)

    return app
