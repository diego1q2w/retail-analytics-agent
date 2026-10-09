"""Request dependencies: the composed services and the authenticated principal.

Identity comes only from ``Authorization: Bearer <token>``; there is no query
parameter, cookie or body field that names a caller.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.interfaces.http.errors import UNAUTHENTICATED, ApiError
from retail_analytics.interfaces.http.services import HttpServices


def services(request: Request) -> HttpServices:
    found = getattr(request.app.state, "services", None)
    if not isinstance(found, HttpServices):
        raise ApiError(503, "unavailable", "The service is not ready.")
    return found


Services = Annotated[HttpServices, Depends(services)]


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        raise UNAUTHENTICATED
    return token


async def principal(
    request: Request,
    composed: Services,
) -> Principal:
    return await composed.authenticator.authenticate(bearer_token(request))


CurrentPrincipal = Annotated[Principal, Depends(principal)]
