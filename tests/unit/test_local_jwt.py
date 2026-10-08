"""Local token authentication: every invalid token is rejected without leaks."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from retail_analytics.adapters.auth.local_jwt import (
    MAX_TOKEN_CHARS,
    LocalJwtAuthority,
)
from retail_analytics.application.authentication import (
    AuthenticationFailed,
    AuthFailure,
)

KEY = "k" * 64
OTHER_KEY = "x" * 64
ISSUER = "retail-analytics-local"
AUDIENCE = "retail-analytics-api"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def authority(key: str = KEY, now: datetime = NOW, **kwargs: Any) -> LocalJwtAuthority:
    options = {"issuer": ISSUER, "audience": AUDIENCE, **kwargs}
    return LocalJwtAuthority(key, clock=lambda: now, **options)


def claims(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "demo-executive-a",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=30)).timestamp()),
        "scope": "analysis:read",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def signed(payload: dict[str, Any], key: str = KEY) -> str:
    return jwt.encode(payload, key, algorithm="HS256")


def rejected(token: str, verifier: LocalJwtAuthority | None = None) -> AuthFailure:
    with pytest.raises(AuthenticationFailed) as caught:
        (verifier or authority()).verify(token)
    if token:
        assert token[:20] not in str(caught.value)
    return caught.value.reason


def test_issued_token_round_trips_identity_and_scopes() -> None:
    token = authority().issue(
        "demo-executive-a", {"analysis:read", "reports:read_own"}, timedelta(hours=1)
    )
    verified = authority().verify(token)
    assert verified.issuer == ISSUER
    assert verified.subject == "demo-executive-a"
    assert verified.scopes == {"analysis:read", "reports:read_own"}
    assert verified.expires_at == NOW + timedelta(hours=1)


def test_token_signed_with_another_key_is_forged() -> None:
    assert rejected(signed(claims(), OTHER_KEY)) is AuthFailure.BAD_SIGNATURE


def test_tampered_payload_fails_signature() -> None:
    header, _, signature = signed(claims()).split(".")
    forged_body = base64.urlsafe_b64encode(
        json.dumps(claims(sub="demo-executive-b")).encode()
    ).rstrip(b"=")
    forged = f"{header}.{forged_body.decode()}.{signature}"
    assert rejected(forged) is AuthFailure.BAD_SIGNATURE


def test_unsigned_alg_none_token_is_rejected() -> None:
    token = jwt.encode(claims(), key="", algorithm="none")
    assert rejected(token) is AuthFailure.BAD_SIGNATURE


def test_other_algorithm_with_same_secret_is_rejected() -> None:
    token = jwt.encode(claims(), KEY, algorithm="HS512")
    assert rejected(token) is AuthFailure.BAD_SIGNATURE


def test_expired_token_fails() -> None:
    expired = claims(
        iat=int((NOW - timedelta(hours=2)).timestamp()),
        exp=int((NOW - timedelta(minutes=5)).timestamp()),
    )
    assert rejected(signed(expired)) is AuthFailure.EXPIRED


def test_token_issued_long_ago_expires_under_the_verifier_clock() -> None:
    token = authority(now=NOW - timedelta(hours=3)).issue(
        "demo-executive-a", {"analysis:read"}, timedelta(hours=1)
    )
    assert rejected(token) is AuthFailure.EXPIRED


def test_expiry_allows_only_small_clock_skew() -> None:
    just_expired = claims(exp=int((NOW - timedelta(seconds=10)).timestamp()))
    assert authority().verify(signed(just_expired)).subject == "demo-executive-a"


def test_wrong_audience_fails() -> None:
    token = signed(claims(aud="some-other-service"))
    assert rejected(token) is AuthFailure.WRONG_AUDIENCE


def test_wrong_issuer_fails() -> None:
    assert rejected(signed(claims(iss="https://elsewhere"))) is AuthFailure.WRONG_ISSUER


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "iat", "exp"])
def test_required_claims_must_be_present(claim: str) -> None:
    assert rejected(signed(claims(**{claim: None}))) is AuthFailure.MISSING_CLAIM


def test_empty_subject_fails() -> None:
    assert rejected(signed(claims(sub=""))) is AuthFailure.MISSING_CLAIM


def test_token_from_the_future_fails() -> None:
    future = claims(
        iat=int((NOW + timedelta(minutes=10)).timestamp()),
        exp=int((NOW + timedelta(minutes=40)).timestamp()),
    )
    assert rejected(signed(future)) is AuthFailure.NOT_YET_VALID


def test_lifetime_above_the_maximum_fails() -> None:
    long_lived = claims(exp=int((NOW + timedelta(days=30)).timestamp()))
    assert rejected(signed(long_lived)) is AuthFailure.LIFETIME_TOO_LONG


@pytest.mark.parametrize("value", ["soon", 1.5, True])
def test_non_integer_times_are_malformed(value: object) -> None:
    assert rejected(signed(claims(exp=value))) in {
        AuthFailure.MALFORMED,
        AuthFailure.EXPIRED,
    }


@pytest.mark.parametrize(
    "token", ["", "not-a-token", "a.b.c", "x" * (MAX_TOKEN_CHARS + 1)]
)
def test_garbage_is_malformed(token: str) -> None:
    assert rejected(token) in {AuthFailure.MALFORMED, AuthFailure.BAD_SIGNATURE}


def test_scope_must_be_a_string() -> None:
    assert rejected(signed(claims(scope=["admin"]))) is AuthFailure.MALFORMED


def test_short_keys_and_bad_lifetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        LocalJwtAuthority("short", issuer=ISSUER, audience=AUDIENCE)
    with pytest.raises(ValueError, match="lifetime"):
        authority().issue("demo-executive-a", (), timedelta(days=2))
    with pytest.raises(ValueError, match="scope"):
        authority().issue("demo-executive-a", {"has space"}, timedelta(minutes=5))


def test_repr_never_shows_the_key() -> None:
    assert KEY not in repr(authority())
