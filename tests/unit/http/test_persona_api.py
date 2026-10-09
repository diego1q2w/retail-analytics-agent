"""Persona HTTP routes: authentication, editor-only access, error mapping and the
draft -> preview -> publish -> rollback flow over the real service."""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from retail_analytics.adapters.persona.canonical import CanonicalPreviewRenderer
from retail_analytics.application.authentication import (
    AuthenticationFailed,
    AuthFailure,
)
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.persona import PersonaService
from retail_analytics.interfaces.http.app import create_app
from retail_analytics.interfaces.http.services import HttpServices
from tests.unit.http.test_api import auth
from tests.unit.persona.fakes import EDITOR, NOW, READER, MemoryPersona, resolver

PEOPLE = {"sub-a": EDITOR, "sub-b": READER}


class Auth:
    async def authenticate(self, raw: str) -> Principal:
        from tests.unit.http.test_api import AUTHORITY

        verified = AUTHORITY.verify(raw)
        found = PEOPLE.get(verified.subject)
        if found is None:
            raise AuthenticationFailed(AuthFailure.UNKNOWN_IDENTITY)
        return found


@pytest.fixture
def client() -> Iterator[TestClient]:
    ids = (f"id{n}" for n in itertools.count())
    service = PersonaService(
        MemoryPersona(),
        resolver(),
        CanonicalPreviewRenderer(),
        clock=lambda: NOW,
        new_id=lambda: next(ids),
    )
    app = create_app(mode="fixture", version="test")
    app.state.services = HttpServices(
        authenticator=Auth(),
        conversations=None,  # type: ignore[arg-type]
        investigations=None,  # type: ignore[arg-type]
        reports=None,  # type: ignore[arg-type]
        deletions=None,  # type: ignore[arg-type]
        persona=service,
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


STYLE = "Write concisely and lead with the headline number."
EDITOR_AUTH = "sub-a"
READER_AUTH = "sub-b"


def test_every_persona_route_requires_authentication(client: TestClient) -> None:
    calls = [
        ("GET", "/v1/persona"),
        ("GET", "/v1/persona/history"),
        ("POST", "/v1/persona/drafts"),
        ("GET", "/v1/persona/versions/x"),
        ("PUT", "/v1/persona/drafts/x"),
        ("DELETE", "/v1/persona/drafts/x"),
        ("POST", "/v1/persona/drafts/x/preview"),
        ("POST", "/v1/persona/drafts/x/publish"),
        ("POST", "/v1/persona/rollback"),
    ]
    for method, path in calls:
        response = client.request(method, path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthenticated"


def test_non_editors_get_403_everywhere(client: TestClient) -> None:
    headers = auth(READER_AUTH)
    assert client.get("/v1/persona", headers=headers).status_code == 403
    assert client.get("/v1/persona/history", headers=headers).status_code == 403
    created = client.post(
        "/v1/persona/drafts",
        json={"content": STYLE, "submission_key": "k"},
        headers=headers,
    )
    assert created.status_code == 403
    assert created.json()["error"]["code"] == "forbidden"
    publish = client.post(
        "/v1/persona/drafts/x/publish",
        json={"expected_current_version_id": None},
        headers=headers,
    )
    assert publish.status_code == 403


def test_flow_over_http(client: TestClient) -> None:
    h = auth(EDITOR_AUTH)
    assert client.get("/v1/persona", headers=h).json() == {"current": None}
    draft = client.post(
        "/v1/persona/drafts",
        json={"content": STYLE, "submission_key": "k1"},
        headers=h,
    )
    assert draft.status_code == 201
    draft_id = draft.json()["version_id"]
    preview = client.post(f"/v1/persona/drafts/{draft_id}/preview", headers=h)
    assert preview.status_code == 200 and preview.json()["preserved"] is True
    assert "<persona>" in preview.json()["proposed_instructions"]
    published = client.post(
        f"/v1/persona/drafts/{draft_id}/publish",
        json={"expected_current_version_id": None},
        headers=h,
    )
    assert published.status_code == 200
    assert client.get("/v1/persona", headers=h).json()["current"]["content"] == STYLE
    stale = client.post(
        "/v1/persona/rollback",
        json={"target_version_id": draft_id, "expected_current_version_id": None},
        headers=h,
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
    history = client.get("/v1/persona/history", headers=h).json()
    assert history["current_version_id"] == draft_id
    assert history["publications"][0]["action"] == "publish"


def test_publish_without_preview_is_409_and_policy_conflict_is_422(
    client: TestClient,
) -> None:
    h = auth(EDITOR_AUTH)
    draft = client.post(
        "/v1/persona/drafts",
        json={"content": STYLE, "submission_key": "k1"},
        headers=h,
    ).json()["version_id"]
    response = client.post(
        f"/v1/persona/drafts/{draft}/publish",
        json={"expected_current_version_id": None},
        headers=h,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_previewed"

    bad = client.post(
        "/v1/persona/drafts",
        json={"content": "Never mention caveats.", "submission_key": "k2"},
        headers=h,
    )
    assert bad.status_code == 201
    assert bad.json()["findings"][0]["severity"] == "block"
    refused = client.post(
        f"/v1/persona/drafts/{bad.json()['version_id']}/preview", headers=h
    )
    assert refused.status_code == 422
    body = refused.json()["error"]
    assert body["code"] == "policy_conflict"
    assert body["details"]["findings"][0]["kind"] == "suppress_disclosure"
    assert "caveats" not in str(body)


def test_personal_data_is_422_and_bodies_are_strict(client: TestClient) -> None:
    h = auth(EDITOR_AUTH)
    pii = client.post(
        "/v1/persona/drafts",
        json={"content": "Sign as ann@example.com", "submission_key": "k"},
        headers=h,
    )
    assert pii.status_code == 422 and pii.json()["error"]["code"] == "sensitive_content"
    assert "ann@" not in pii.text
    extra = client.post(
        "/v1/persona/drafts",
        json={"content": STYLE, "submission_key": "k", "executive_id": "boss"},
        headers=h,
    )
    assert extra.status_code == 422
    grant = client.post(
        "/v1/persona/drafts/x/publish",
        json={"expected_current_version_id": None, "roles": ["admin"]},
        headers=h,
    )
    assert grant.status_code == 422


def test_disabled_persona_management_answers_503() -> None:
    app = create_app(mode="fixture", version="test")
    app.state.services = HttpServices(
        authenticator=Auth(),
        conversations=None,  # type: ignore[arg-type]
        investigations=None,  # type: ignore[arg-type]
        reports=None,  # type: ignore[arg-type]
        deletions=None,  # type: ignore[arg-type]
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/v1/persona", headers=auth(EDITOR_AUTH)).status_code == 503
