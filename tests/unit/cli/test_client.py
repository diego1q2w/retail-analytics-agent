from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from retail_analytics.interfaces.cli.client import ApiClient, ApiError, Unreachable
from tests.unit.cli.fake_backend import TOKEN, Backend, error


def test_write_is_resent_with_the_same_submission_key_after_503_and_lost_response() -> (
    None
):
    backend = Backend()

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("lost", request=request)

    outcomes: list[Callable[[httpx.Request], httpx.Response]] = [
        lambda r: error(503, "unavailable", "retry"),
        lost,
        lambda r: httpx.Response(
            202, json={"run_id": "r1", "status": "running", "created": True}
        ),
    ]
    backend.overrides[("POST", "/v1/sessions/s1/runs")] = lambda r: outcomes.pop(0)(r)
    api = ApiClient(backend.client(), sleep=lambda _s: None)
    handle = api.start_run("s1", "hello", "key-1")
    assert handle["run_id"] == "r1"
    sent = backend.calls("POST", "/runs")
    assert len(sent) == 3
    assert {r.content for r in sent} == {sent[0].content}
    assert b'"key-1"' in sent[0].content


def test_gives_up_with_unreachable_after_bounded_resends() -> None:
    backend = Backend()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    backend.overrides[("GET", "/v1/sessions")] = refuse
    api = ApiClient(backend.client(), resends=2, sleep=lambda _s: None)
    with pytest.raises(Unreachable):
        api.list_sessions()
    assert len(backend.requests) == 3


def test_missing_token_fails_before_any_request_and_never_leaks_a_token() -> None:
    backend = Backend()
    api = ApiClient(backend.client(token=None))
    with pytest.raises(ApiError) as missing:
        api.list_sessions()
    assert missing.value.code == "no_token"
    assert backend.requests == []

    wrong = ApiClient(backend.client(token="wrong-token-abc"))  # noqa: S106
    with pytest.raises(ApiError) as denied:
        wrong.list_sessions()
    assert denied.value.code == "unauthenticated"
    assert "wrong-token-abc" not in str(denied.value)
    assert TOKEN not in str(denied.value)


def test_non_json_error_bodies_become_a_structured_error() -> None:
    backend = Backend()
    backend.overrides[("GET", "/v1/sessions")] = lambda r: httpx.Response(
        502, text="<html>bad gateway</html>"
    )
    api = ApiClient(backend.client(), sleep=lambda _s: None)
    with pytest.raises(ApiError) as caught:
        api.list_sessions()
    assert caught.value.code == "unexpected_response"
    assert "bad gateway" not in caught.value.message


def test_confirm_deletion_is_not_resent_after_a_lost_response() -> None:
    backend = Backend()

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("lost", request=request)

    backend.overrides[("POST", "/v1/deletion-proposals/p1/confirm")] = lost
    api = ApiClient(backend.client(), sleep=lambda _s: None)
    with pytest.raises(Unreachable):
        api.confirm_deletion("p1")
    assert len(backend.calls("POST", "/confirm")) == 1
