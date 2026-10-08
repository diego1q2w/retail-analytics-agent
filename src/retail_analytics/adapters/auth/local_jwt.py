"""Locally signed JWTs (HS256): simulated authentication for development.

This stands in for a company identity provider. It issues and verifies tokens
with one shared secret held only by the backend and the developer's dev
command. A production deployment replaces it with a verifier for the
provider's asymmetric keys (JWKS) behind the same ``TokenVerifier`` port; the
authorization path after verification does not change.

Verification accepts exactly one algorithm (no ``none``, no algorithm taken
from the token), requires ``iss``, ``aud``, ``sub``, ``iat`` and ``exp``,
bounds the token lifetime and never echoes token content in errors.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt

from retail_analytics.application.authentication import (
    AuthenticationFailed,
    AuthFailure,
)
from retail_analytics.application.contracts.authentication import VerifiedToken

ALGORITHM: Final = "HS256"
MIN_KEY_BYTES: Final = 32
MAX_TOKEN_CHARS: Final = 8192
DEFAULT_LEEWAY: Final = timedelta(seconds=30)
MAX_LIFETIME: Final = timedelta(hours=24)
_REQUIRED_CLAIMS: Final = ["iss", "aud", "sub", "iat", "exp"]
_SCOPE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]{1,64}$")

_FAILURES: Final[tuple[tuple[type[jwt.PyJWTError], AuthFailure], ...]] = (
    (jwt.InvalidSignatureError, AuthFailure.BAD_SIGNATURE),
    (jwt.InvalidAlgorithmError, AuthFailure.BAD_SIGNATURE),
    (jwt.ExpiredSignatureError, AuthFailure.EXPIRED),
    (jwt.ImmatureSignatureError, AuthFailure.NOT_YET_VALID),
    (jwt.InvalidAudienceError, AuthFailure.WRONG_AUDIENCE),
    (jwt.InvalidIssuerError, AuthFailure.WRONG_ISSUER),
    (jwt.MissingRequiredClaimError, AuthFailure.MISSING_CLAIM),
)


def _now() -> datetime:
    return datetime.now(UTC)


class LocalJwtAuthority:
    """Issues and verifies local tokens. Holds the key; does not repr it."""

    __slots__ = ("_audience", "_clock", "_issuer", "_key", "_leeway")

    def __init__(
        self,
        signing_key: str,
        *,
        issuer: str,
        audience: str,
        leeway: timedelta = DEFAULT_LEEWAY,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        key = signing_key.encode()
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"signing key must be at least {MIN_KEY_BYTES} bytes")
        if not issuer or not audience:
            raise ValueError("issuer and audience are required")
        self._key = key
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway
        self._clock = clock

    def __repr__(self) -> str:
        return (
            f"LocalJwtAuthority(issuer={self._issuer!r}, audience={self._audience!r})"
        )

    @property
    def issuer(self) -> str:
        return self._issuer

    def issue(self, subject: str, scopes: Iterable[str], lifetime: timedelta) -> str:
        if not subject:
            raise ValueError("subject is required")
        if not timedelta(0) < lifetime <= MAX_LIFETIME:
            raise ValueError(f"lifetime must be positive and at most {MAX_LIFETIME}")
        scope_list = sorted(set(scopes))
        if not all(_SCOPE.fullmatch(scope) for scope in scope_list):
            raise ValueError("invalid scope value")
        now = self._clock()
        claims = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": subject,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + lifetime).timestamp()),
            "jti": uuid.uuid4().hex,
            "scope": " ".join(scope_list),
        }
        return jwt.encode(claims, self._key, algorithm=ALGORITHM)

    def verify(self, token: str) -> VerifiedToken:
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise AuthenticationFailed(AuthFailure.MALFORMED)
        now = self._clock()
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                # Time claims are checked below against the injected clock.
                options={
                    "require": _REQUIRED_CLAIMS,
                    "verify_signature": True,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError as error:
            raise AuthenticationFailed(_reason(error)) from None
        return self._checked(claims, now)

    def _checked(self, claims: dict[str, Any], now: datetime) -> VerifiedToken:
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject:
            raise AuthenticationFailed(AuthFailure.MISSING_CLAIM)
        issued, expires = _instant(claims["iat"]), _instant(claims["exp"])
        not_before = _instant(claims.get("nbf", claims["iat"]))
        if expires <= now - self._leeway:
            raise AuthenticationFailed(AuthFailure.EXPIRED)
        if max(issued, not_before) > now + self._leeway:
            raise AuthenticationFailed(AuthFailure.NOT_YET_VALID)
        if expires - issued > MAX_LIFETIME:
            raise AuthenticationFailed(AuthFailure.LIFETIME_TOO_LONG)
        scope = claims.get("scope", "")
        if not isinstance(scope, str):
            raise AuthenticationFailed(AuthFailure.MALFORMED)
        return VerifiedToken(
            issuer=self._issuer,
            subject=subject,
            scopes=frozenset(scope.split()),
            expires_at=expires,
        )


def _instant(value: object) -> datetime:
    # bool is an int subclass; a boolean timestamp is malformed.
    if not isinstance(value, int) or isinstance(value, bool):
        raise AuthenticationFailed(AuthFailure.MALFORMED)
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        raise AuthenticationFailed(AuthFailure.MALFORMED) from None


def _reason(error: jwt.PyJWTError) -> AuthFailure:
    for kind, reason in _FAILURES:
        if isinstance(error, kind):
            return reason
    return AuthFailure.MALFORMED
