"""Authenticating an executive from a bearer token.

A token proves *who* is calling (issuer + subject) and may narrow *what* the
call may do (its ``scope``). It never carries product entitlements or roles:
those are server-side and resolved fresh by ``AccessResolver``. Any problem
with the token, or a subject with no active executive, fails authentication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from retail_analytics.application.authorization import ExecutiveDirectory, Principal


class AuthFailure(StrEnum):
    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    WRONG_AUDIENCE = "wrong_audience"
    WRONG_ISSUER = "wrong_issuer"
    MISSING_CLAIM = "missing_claim"
    LIFETIME_TOO_LONG = "lifetime_too_long"
    UNKNOWN_IDENTITY = "unknown_identity"


class AuthenticationFailed(Exception):
    """The caller is not authenticated.

    ``reason`` is for logs and tests; clients get one uniform answer. Neither
    carries the token or its claims.
    """

    def __init__(self, reason: AuthFailure) -> None:
        self.reason = reason
        super().__init__(f"authentication failed: {reason.value}")


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    issuer: str
    subject: str
    scopes: frozenset[str]
    expires_at: datetime


class TokenVerifier(Protocol):
    def verify(self, token: str) -> VerifiedToken:
        """Check signature, issuer, audience and validity period.

        Raises ``AuthenticationFailed``; never returns unverified claims.
        """
        ...


class Authenticator:
    def __init__(self, verifier: TokenVerifier, directory: ExecutiveDirectory) -> None:
        self._verifier = verifier
        self._directory = directory

    async def authenticate(self, token: str) -> Principal:
        verified = self._verifier.verify(token)
        access = await self._directory.find_by_subject(
            verified.issuer, verified.subject
        )
        if access is None or not access.active:
            raise AuthenticationFailed(AuthFailure.UNKNOWN_IDENTITY)
        return Principal(executive_id=access.executive_id, scopes=verified.scopes)
