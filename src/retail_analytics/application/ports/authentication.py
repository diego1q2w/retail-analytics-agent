from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.authentication import VerifiedToken


class TokenVerifier(Protocol):
    def verify(self, token: str) -> VerifiedToken:
        """Check signature, issuer, audience and validity period.

        Raises ``AuthenticationFailed``; never returns unverified claims.
        """
        ...
