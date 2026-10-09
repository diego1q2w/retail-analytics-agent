"""HTTP contract tests over ASGI: authentication on every route, uniform error
mapping, strict bodies, explicit deletion confirmation and the SSE stream.

Application services are fakes that keep the real contracts (owner-only
records raise ``AccessDenied``); the real services run in the Docker tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from retail_analytics.adapters.auth.local_jwt import LocalJwtAuthority
from retail_analytics.application.authentication import (
    AuthenticationFailed,
    AuthFailure,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.conversations import (
    EventBatch,
    ReleasedText,
    RunView,
    SessionOverview,
)
from retail_analytics.application.contracts.persistence import (
    ActiveRunExists,
    IdempotencyConflict,
    RecordNotFound,
)
from retail_analytics.application.contracts.progress import (
    EventKind,
    ProgressEvent,
    ProgressUpdate,
)
from retail_analytics.application.contracts.report_deletion import (
    DeletionPreview,
    DeletionResult,
)
from retail_analytics.application.contracts.reports import (
    ExportedReport,
    ReportDocument,
    ReportListing,
    ReportSearchResult,
)
from retail_analytics.application.investigations import (
    InputReceipt,
    RunHandle,
    RunNotActive,
    SubmitMode,
)
from retail_analytics.domain.conversation import Session
from retail_analytics.domain.investigations import InputKind
from retail_analytics.domain.report_deletion import (
    DeletionError,
    DeletionErrorCode,
    ProposalStatus,
)
from retail_analytics.domain.reports import (
    ReportError,
    ReportErrorCode,
    ReportVersion,
)
from retail_analytics.domain.runs import Run, RunStatus
from retail_analytics.interfaces.http.app import create_app
from retail_analytics.interfaces.http.services import HttpServices, StreamSettings

T0 = datetime(2026, 10, 9, 12, tzinfo=UTC)
KEY = "unit-test-signing-key-" + "x" * 32
AUTHORITY = LocalJwtAuthority(KEY, issuer="iss", audience="aud")
ALICE = Principal("exec-a", frozenset({"analysis:read"}))
BOB = Principal("exec-b", frozenset({"analysis:read"}))
SUBJECTS = {"sub-a": ALICE, "sub-b": BOB}


def token(subject: str = "sub-a") -> str:
    return AUTHORITY.issue(subject, ["analysis:read"], timedelta(minutes=5))


def auth(subject: str = "sub-a") -> dict[str, str]:
    return {"Authorization": f"Bearer {token(subject)}"}


class Auth:
    async def authenticate(self, raw: str) -> Principal:
        verified = AUTHORITY.verify(raw)
        found = SUBJECTS.get(verified.subject)
        if found is None:
            raise AuthenticationFailed(AuthFailure.UNKNOWN_IDENTITY)
        return found


def run(run_id: str = "run-1", status: RunStatus = RunStatus.RUNNING) -> Run:
    return Run(
        run_id=run_id,
        session_id="ses-1",
        requested_by=ALICE.executive_id,
        trigger_message_id="m",
        submission_key="k",
        status=status,
        created_at=T0,
        updated_at=T0,
    )


def progress(
    sequence: int, kind: EventKind = EventKind.ANALYSIS_PROGRESS
) -> ProgressEvent:
    update = ProgressUpdate(
        correlation=Correlation(session_id="ses-1", run_id="run-1"),
        kind=kind,
        summary=f"step {sequence}",
    )
    return ProgressEvent.stamp(
        update, event_id=f"evt-{sequence}", sequence=sequence, occurred_at=T0
    )


def owned(principal: Principal, kind: str, key: str) -> None:
    if principal.executive_id != ALICE.executive_id or key.startswith("missing"):
        raise AccessDenied(kind, key)


@dataclass
class Conversations:
    events_list: list[ProgressEvent] = field(default_factory=list)
    status: RunStatus = RunStatus.RUNNING
    # Raise this on the n-th events() call (1-based).
    fail_on_call: tuple[int, Exception] | None = None
    calls: int = 0

    async def open_session(self, principal: Principal, submission_key: str) -> Session:
        return Session(f"ses-{submission_key}", principal.executive_id, T0, T0)

    async def list_sessions(
        self, principal: Principal, *, limit: int = 20, offset: int = 0
    ) -> tuple[Session, ...]:
        return (Session("ses-1", principal.executive_id, T0, T0),)

    async def session(
        self, principal: Principal, session_id: str, *, run_limit: int = 20
    ) -> SessionOverview:
        owned(principal, "session", session_id)
        return SessionOverview(
            Session(session_id, ALICE.executive_id, T0, T0), (run(),)
        )

    async def run_view(self, principal: Principal, run_id: str) -> RunView:
        owned(principal, "run", run_id)
        return RunView(
            run(run_id, RunStatus.COMPLETED), None, ReleasedText("Revenue rose 4%.")
        )

    async def events(
        self,
        principal: Principal,
        run_id: str,
        *,
        after_event_id: str | None = None,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> EventBatch:
        self.calls += 1
        if self.fail_on_call and self.fail_on_call[0] == self.calls:
            raise self.fail_on_call[1]
        owned(principal, "run", run_id)
        after = 0
        if after_event_id is not None:
            found = [
                e.sequence for e in self.events_list if e.event_id == after_event_id
            ]
            if not found:
                raise RecordNotFound("run event", after_event_id)
            after = found[0]
        later = tuple(e for e in self.events_list if e.sequence > after)
        return EventBatch(later, self.status, True)


@dataclass
class Investigations:
    error: Exception | None = None
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def _record(self, name: str, principal: Principal, **kw: object) -> None:
        self.calls.append((name, kw))
        if self.error is not None:
            raise self.error
        key = kw.get("session_id") or kw.get("run_id")
        owned(principal, "run", str(key))

    async def start(
        self, principal: Principal, *, session_id: str, text: str, submission_key: str
    ) -> RunHandle:
        self._record("start", principal, session_id=session_id, text=text)
        return RunHandle("run-1", RunStatus.RUNNING, True)

    async def submit(
        self,
        principal: Principal,
        *,
        session_id: str,
        text: str,
        submission_key: str,
        mode: SubmitMode = SubmitMode.STEER,
    ) -> InputReceipt:
        self._record("submit", principal, session_id=session_id, mode=mode)
        kind = InputKind.QUEUED if mode is SubmitMode.QUEUE else InputKind.STEERING
        return InputReceipt("in-1", kind, None if kind is InputKind.QUEUED else "run-1")

    async def steer(
        self, principal: Principal, *, run_id: str, text: str, submission_key: str
    ) -> InputReceipt:
        self._record("steer", principal, run_id=run_id)
        return InputReceipt("in-2", InputKind.STEERING, run_id)

    async def answer(
        self,
        principal: Principal,
        *,
        run_id: str,
        question_id: str,
        text: str,
        submission_key: str,
    ) -> InputReceipt:
        self._record("answer", principal, run_id=run_id, question_id=question_id)
        return InputReceipt("in-3", InputKind.ANSWER, run_id)

    async def cancel(self, principal: Principal, *, run_id: str) -> Run:
        self._record("cancel", principal, run_id=run_id)
        return run(run_id, RunStatus.CANCELLING)


class Reports:
    error: Exception | None = None

    def _check(self, principal: Principal, report_id: str = "rep-1") -> None:
        if self.error is not None:
            raise self.error
        owned(principal, "report", report_id)

    async def list_reports(
        self,
        principal: Principal,
        *,
        session_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ReportListing, ...]:
        self._check(principal)
        return ()

    async def versions(
        self, principal: Principal, report_id: str
    ) -> tuple[ReportListing, ...]:
        self._check(principal, report_id)
        return ()

    async def read(
        self, principal: Principal, report_id: str, version: int | None = None
    ) -> ReportDocument:
        self._check(principal, report_id)
        raise ReportError(ReportErrorCode.ACCESS_CHANGED, "Access changed.")

    async def export(
        self, principal: Principal, report_id: str, version: int | None = None
    ) -> ExportedReport:
        self._check(principal, report_id)
        version_record = ReportVersion(
            report_id=report_id,
            version=1,
            owner_id=ALICE.executive_id,
            session_id=None,
            run_id=None,
            artifact_version=1,
            title="t",
            evidence_ids=(),
            scope_digest="d",
            authorization_version=1,
            draft_digest="d",
            created_at=T0,
        )
        return ExportedReport(
            "report-rep-1-v1.md", "text/markdown", b"# R\n", version_record
        )

    async def search(
        self,
        principal: Principal,
        query: str,
        *,
        session_id: str | None = None,
        limit: int = 10,
    ) -> ReportSearchResult:
        self._check(principal)
        return ReportSearchResult((), 0, 0, False)


@dataclass
class Deletions:
    confirmed: list[str] = field(default_factory=list)
    error: Exception | None = None
    pending: dict[str, list[DeletionPreview]] = field(default_factory=dict)

    async def list_pending(self, principal: Principal) -> tuple[DeletionPreview, ...]:
        return tuple(self.pending.get(principal.executive_id, ()))

    def _preview(self, proposal_id: str) -> DeletionPreview:
        return DeletionPreview(proposal_id, ProposalStatus.PENDING, T0, ())

    async def preview(self, principal: Principal, proposal_id: str) -> DeletionPreview:
        owned(principal, "deletion proposal", proposal_id)
        return self._preview(proposal_id)

    async def confirm(self, principal: Principal, proposal_id: str) -> DeletionResult:
        owned(principal, "deletion proposal", proposal_id)
        if self.error is not None:
            raise self.error
        self.confirmed.append(proposal_id)
        return DeletionResult(proposal_id, ("rep-1",), T0, T0 + timedelta(days=7))

    async def cancel(self, principal: Principal, proposal_id: str) -> DeletionPreview:
        owned(principal, "deletion proposal", proposal_id)
        return self._preview(proposal_id)


@dataclass
class World:
    conversations: Conversations = field(default_factory=Conversations)
    investigations: Investigations = field(default_factory=Investigations)
    reports: Reports = field(default_factory=Reports)
    deletions: Deletions = field(default_factory=Deletions)

    def app(self, stream: StreamSettings | None = None) -> FastAPI:
        app = create_app(
            mode="fixture",
            version="test",
            stream=stream
            or StreamSettings(
                poll_seconds=0.01,
                heartbeat_seconds=5,
                max_seconds=2,
                terminal_grace_seconds=0.05,
            ),
        )
        app.state.services = HttpServices(
            authenticator=Auth(),
            conversations=self.conversations,
            investigations=self.investigations,
            reports=self.reports,
            deletions=self.deletions,
        )
        return app


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def client(world: World) -> Iterator[TestClient]:
    with TestClient(world.app(), raise_server_exceptions=False) as c:
        yield c


ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("POST", "/v1/sessions", {"submission_key": "k1"}),
    ("GET", "/v1/sessions", None),
    ("GET", "/v1/sessions/ses-1", None),
    ("POST", "/v1/sessions/ses-1/runs", {"text": "Sales?", "submission_key": "k"}),
    ("POST", "/v1/sessions/ses-1/messages", {"text": "Hi", "submission_key": "k"}),
    ("GET", "/v1/runs/run-1", None),
    ("POST", "/v1/runs/run-1/steer", {"text": "Hi", "submission_key": "k"}),
    (
        "POST",
        "/v1/runs/run-1/answers",
        {"question_id": "q", "text": "Hi", "submission_key": "k"},
    ),
    ("POST", "/v1/runs/run-1/cancel", None),
    ("GET", "/v1/runs/run-1/events", None),
    ("GET", "/v1/reports", None),
    ("GET", "/v1/reports/search?q=revenue", None),
    ("GET", "/v1/reports/rep-1", None),
    ("GET", "/v1/reports/rep-1/versions", None),
    ("GET", "/v1/reports/rep-1/export", None),
    ("GET", "/v1/deletion-proposals?status=pending", None),
    ("GET", "/v1/deletion-proposals/p-1", None),
    ("POST", "/v1/deletion-proposals/p-1/confirm", {"confirm": True}),
    ("POST", "/v1/deletion-proposals/p-1/cancel", None),
]

# Routes addressing one owned record (lists and search only return the
# caller's own records).
OWNED_ROUTES = [
    r for r in ROUTES if r[1] not in ("/v1/sessions", "/v1/reports") and "?" not in r[1]
]

BAD_AUTH = [
    {},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer not-a-jwt"},
    {"Authorization": f"Basic {token()}"},
    {"Authorization": f"Bearer {token('sub-unknown')}"},
    {
        "Authorization": "Bearer "
        + LocalJwtAuthority(
            KEY, issuer="iss", audience="aud", clock=lambda: T0 - timedelta(days=30)
        ).issue("sub-a", ["analysis:read"], timedelta(minutes=5))
    },
    {
        "Authorization": "Bearer "
        + LocalJwtAuthority(
            "other-key-" + "y" * 32, issuer="iss", audience="aud"
        ).issue("sub-a", ["analysis:read"], timedelta(minutes=5))
    },
]


def test_openapi_lists_routes_and_has_no_restore(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for method, path, _ in ROUTES:
        template = (
            path.split("?")[0]
            .replace("ses-1", "{session_id}")
            .replace("run-1", "{run_id}")
            .replace("rep-1", "{report_id}")
            .replace("p-1", "{proposal_id}")
        )
        assert method.lower() in paths[template], template
    assert not any("restore" in p for p in paths)
    assert client.post("/v1/reports/rep-1/restore", headers=auth()).status_code in (
        404,
        405,
    )


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
@pytest.mark.parametrize("headers", BAD_AUTH)
def test_every_route_requires_a_valid_bearer_token(
    client: TestClient,
    world: World,
    method: str,
    path: str,
    body: dict[str, object] | None,
    headers: dict[str, str],
) -> None:
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "unauthenticated",
            "message": "A valid bearer token is required.",
            "details": {},
        }
    }
    assert response.headers["www-authenticate"] == "Bearer"
    assert world.investigations.calls == []
    assert world.deletions.confirmed == []


def test_token_in_query_string_is_not_accepted(client: TestClient) -> None:
    response = client.get(f"/v1/sessions?access_token={token()}")
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), OWNED_ROUTES)
def test_wrong_owner_is_indistinguishable_from_missing(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    as_bob = client.request(method, path, json=body, headers=auth("sub-b"))
    missing = client.request(
        method,
        path.replace("ses-1", "missing-s")
        .replace("run-1", "missing-r")
        .replace("rep-1", "missing-rep")
        .replace("p-1", "missing-p"),
        json=body,
        headers=auth(),
    )
    assert as_bob.status_code == missing.status_code == 404
    assert as_bob.json() == missing.json()
    assert as_bob.json()["error"]["code"] == "not_found"


def test_body_cannot_carry_identity_or_unknown_fields(
    client: TestClient, world: World
) -> None:
    response = client.post(
        "/v1/sessions/ses-1/runs",
        json={"text": "Sales?", "submission_key": "k", "executive_id": "exec-b"},
        headers=auth(),
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]["problems"][0]["location"][-1] == "executive_id"
    assert world.investigations.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"text": "", "submission_key": "k"},
        {"text": "x" * 8001, "submission_key": "k"},
        {"text": "ok", "submission_key": "bad key with spaces"},
        {"text": "ok"},
    ],
)
def test_malformed_run_requests_are_structured(
    client: TestClient, body: dict[str, object]
) -> None:
    response = client.post("/v1/sessions/ses-1/runs", json=body, headers=auth())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "x" * 100 not in response.text


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ActiveRunExists("ses-1", "run-0"), 409, "active_run_exists"),
        (RunNotActive("run-1"), 409, "run_not_active"),
        (IdempotencyConflict("run", "k"), 409, "idempotency_conflict"),
        (AccessDenied("permission", "analysis:read"), 403, "forbidden"),
        (ValueError("message must not be empty"), 422, "invalid_request"),
        (RuntimeError("secret internals"), 503, "unavailable"),
    ],
)
def test_application_errors_map_consistently(
    client: TestClient, world: World, error: Exception, status: int, code: str
) -> None:
    world.investigations.error = error
    response = client.post(
        "/v1/sessions/ses-1/runs",
        json={"text": "Sales?", "submission_key": "k"},
        headers=auth(),
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "secret internals" not in response.text
    if code == "active_run_exists":
        assert response.json()["error"]["details"] == {"active_run_id": "run-0"}


def test_run_commands_and_view(client: TestClient, world: World) -> None:
    started = client.post(
        "/v1/sessions/ses-1/runs",
        json={"text": "Sales?", "submission_key": "k"},
        headers=auth(),
    )
    assert started.status_code == 202
    assert started.json() == {"run_id": "run-1", "status": "running", "created": True}
    queued = client.post(
        "/v1/sessions/ses-1/messages",
        json={"text": "Also returns", "submission_key": "k2", "mode": "queue"},
        headers=auth(),
    )
    assert queued.json() == {"input_id": "in-1", "kind": "queued", "run_id": None}
    assert world.investigations.calls[-1][1]["mode"] is SubmitMode.QUEUE
    answered = client.post(
        "/v1/runs/run-1/answers",
        json={"question_id": "q1", "text": "Last month", "submission_key": "k3"},
        headers=auth(),
    )
    assert answered.json()["kind"] == "answer"
    cancelled = client.post("/v1/runs/run-1/cancel", headers=auth())
    assert cancelled.json()["run"]["status"] == "cancelling"
    view = client.get("/v1/runs/run-1", headers=auth()).json()
    assert view["status"] == "completed"
    assert view["answer"] == {"text": "Revenue rose 4%.", "withheld": False}
    sessions = client.get("/v1/sessions", headers=auth()).json()["sessions"]
    assert [s["session_id"] for s in sessions] == ["ses-1"]


def test_reports_routes_and_errors(client: TestClient, world: World) -> None:
    exported = client.get("/v1/reports/rep-1/export", headers=auth())
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.content == b"# R\n"
    changed = client.get("/v1/reports/rep-1", headers=auth())
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "access_changed"
    assert client.get("/v1/reports/search?q=", headers=auth()).status_code == 422


def test_deletion_confirmation_needs_explicit_approval(
    client: TestClient, world: World
) -> None:
    for body in (None, {}, {"confirm": False}, {"confirm": "yes"}):
        response = client.post(
            "/v1/deletion-proposals/p-1/confirm", json=body, headers=auth()
        )
        assert response.status_code == 422
    assert world.deletions.confirmed == []
    confirmed = client.post(
        "/v1/deletion-proposals/p-1/confirm", json={"confirm": True}, headers=auth()
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["report_ids"] == ["rep-1"]
    assert world.deletions.confirmed == ["p-1"]
    world.deletions.error = DeletionError(DeletionErrorCode.EXPIRED, "It expired.")
    expired = client.post(
        "/v1/deletion-proposals/p-1/confirm", json={"confirm": True}, headers=auth()
    )
    assert expired.status_code == 410
    assert expired.json()["error"] == {
        "code": "expired",
        "message": "It expired.",
        "details": {},
    }


def test_pending_deletion_proposals_are_listed_for_the_caller_only(
    client: TestClient, world: World
) -> None:
    world.deletions.pending["exec-a"] = [
        DeletionPreview("p-1", ProposalStatus.PENDING, T0, ())
    ]
    listed = client.get("/v1/deletion-proposals?status=pending", headers=auth())
    assert listed.status_code == 200
    assert [p["proposal_id"] for p in listed.json()["proposals"]] == ["p-1"]
    assert client.get("/v1/deletion-proposals", headers=auth()).status_code == 200
    other = client.get("/v1/deletion-proposals?status=pending", headers=auth("sub-b"))
    assert other.json() == {"proposals": []}
    for status in ("confirmed", "all", ""):
        bad = client.get(f"/v1/deletion-proposals?status={status}", headers=auth())
        assert bad.status_code == 422
    assert world.deletions.confirmed == []


def test_v1_is_unavailable_without_services() -> None:
    app = create_app(mode="fixture", version="test")
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        response = c.get("/v1/sessions", headers=auth())
        assert response.status_code == 503


# --- SSE ----------------------------------------------------------------------


def parse(text: str) -> list[dict[str, str]]:
    messages = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                fields["comment"] = line[1:].strip()
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if fields:
            messages.append(fields)
    return messages


def stream(client: TestClient, url: str, **headers: str) -> list[dict[str, str]]:
    with client.stream("GET", url, headers={**auth(), **headers}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return parse(response.read().decode())


def test_sse_streams_in_order_and_ends_after_terminal_event(
    client: TestClient, world: World
) -> None:
    world.conversations.events_list = [
        progress(1),
        progress(2),
        progress(3, EventKind.RUN_COMPLETED),
    ]
    world.conversations.status = RunStatus.COMPLETED
    messages = stream(client, "/v1/runs/run-1/events")
    assert messages[0] == {"retry": "2000"}
    events = [m for m in messages if "id" in m]
    assert [m["id"] for m in events] == ["evt-1", "evt-2", "evt-3"]
    assert events[-1]["event"] == "run.completed"
    assert messages[-1]["event"] == "end"
    assert '"status": "completed"' in messages[-1]["data"]


@pytest.mark.parametrize("how", ["header", "query"])
def test_sse_resumes_after_last_event_id_without_repeats(
    client: TestClient, world: World, how: str
) -> None:
    world.conversations.events_list = [
        progress(1),
        progress(2),
        progress(3, EventKind.RUN_COMPLETED),
    ]
    world.conversations.status = RunStatus.COMPLETED
    if how == "header":
        messages = stream(client, "/v1/runs/run-1/events", **{"Last-Event-ID": "evt-1"})
    else:
        messages = stream(client, "/v1/runs/run-1/events?after=evt-1")
    assert [m["id"] for m in messages if "id" in m] == ["evt-2", "evt-3"]


def test_sse_cursor_and_owner_errors_are_answered_before_streaming(
    client: TestClient,
) -> None:
    bad = client.get(
        "/v1/runs/run-1/events", headers={**auth(), "Last-Event-ID": "bad id\n"}
    )
    assert bad.status_code == 400
    foreign = client.get(
        "/v1/runs/run-1/events", headers={**auth(), "Last-Event-ID": "evt-99"}
    )
    assert foreign.status_code == 400
    assert foreign.json()["error"]["code"] == "invalid_cursor"
    other = client.get("/v1/runs/run-1/events", headers=auth("sub-b"))
    assert other.status_code == 404


def test_sse_sends_heartbeats_and_closes_at_max_duration(world: World) -> None:
    world.conversations.events_list = [progress(1)]
    world.conversations.status = RunStatus.WAITING_FOR_INPUT
    app = world.app(
        StreamSettings(
            poll_seconds=0.01,
            heartbeat_seconds=0.05,
            max_seconds=0.3,
            terminal_grace_seconds=0.05,
        )
    )
    with TestClient(app) as c:
        messages = stream(c, "/v1/runs/run-1/events")
    assert any(m.get("comment") == "keepalive" for m in messages)
    assert not any(m.get("event") == "end" for m in messages)


def test_sse_stops_when_authority_is_revoked_mid_stream(
    client: TestClient, world: World
) -> None:
    world.conversations.events_list = [progress(1)]
    world.conversations.fail_on_call = (3, AccessDenied("run", "run-1"))
    messages = stream(client, "/v1/runs/run-1/events")
    assert [m["id"] for m in messages if "id" in m] == ["evt-1"]
    assert messages[-1]["event"] == "error"
    assert '"code": "not_found"' in messages[-1]["data"]


def test_sse_ends_for_a_finished_run_without_a_terminal_event(
    client: TestClient, world: World
) -> None:
    world.conversations.events_list = [progress(1)]
    world.conversations.status = RunStatus.FAILED
    messages = stream(client, "/v1/runs/run-1/events", **{"Last-Event-ID": "evt-1"})
    assert not [m for m in messages if "id" in m]
    assert messages[-1]["event"] == "end"
