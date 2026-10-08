"""Composition of authentication and authorization.

``build_access`` is how entry points (API, worker, dev tools) obtain the
authenticator, the ownership guard and the resolver that produces every
authorized ``ExecutionContext``.
"""

from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.adapters.auth.local_jwt import LocalJwtAuthority
from retail_analytics.application.authentication import Authenticator, TokenVerifier
from retail_analytics.application.authorization import AccessResolver, OwnershipGuard
from retail_analytics.bootstrap.config import BackendSettings, ConfigError
from retail_analytics.bootstrap.persistence import Persistence


@dataclass(frozen=True)
class AccessServices:
    authenticator: Authenticator
    guard: OwnershipGuard
    resolver: AccessResolver


def local_token_authority(settings: BackendSettings) -> LocalJwtAuthority:
    if settings.auth_signing_key is None:
        raise ConfigError(
            ["RETAIL_ANALYTICS_AUTH_SIGNING_KEY: required for token authentication"]
        )
    return LocalJwtAuthority(
        settings.auth_signing_key.get_secret_value(),
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
    )


def build_access(persistence: Persistence, verifier: TokenVerifier) -> AccessServices:
    guard = OwnershipGuard(
        persistence.sessions, persistence.runs, persistence.tool_executions
    )
    return AccessServices(
        authenticator=Authenticator(verifier, persistence.executives),
        guard=guard,
        resolver=AccessResolver(persistence.executives, guard),
    )
