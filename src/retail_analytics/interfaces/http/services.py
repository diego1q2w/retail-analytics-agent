"""What the HTTP interface calls: narrow views of the application use cases.

Bootstrap supplies the real services (``ConversationService``,
``InvestigationControl``, ``ReportService``, ``ReportDeletionService`` and the
``Authenticator``); tests may supply fakes with the same contracts. There is
deliberately no restore operation: restoring deleted reports is an operator
CLI action (``retail-analytics-maintenance restore``).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.conversations import (
    EventBatch,
    RunView,
    SessionOverview,
)
from retail_analytics.application.contracts.persona import (
    PersonaHistory,
    PersonaPreview,
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
    SubmitMode,
)
from retail_analytics.domain.conversation import Session
from retail_analytics.domain.persona import PersonaVersion, Publication
from retail_analytics.domain.runs import Run


class Authenticates(Protocol):
    async def authenticate(self, token: str) -> Principal: ...


class Conversations(Protocol):
    async def open_session(
        self, principal: Principal, submission_key: str
    ) -> Session: ...

    async def list_sessions(
        self, principal: Principal, *, limit: int = ..., offset: int = ...
    ) -> tuple[Session, ...]: ...

    async def session(
        self, principal: Principal, session_id: str, *, run_limit: int = ...
    ) -> SessionOverview: ...

    async def run_view(self, principal: Principal, run_id: str) -> RunView: ...

    async def events(
        self,
        principal: Principal,
        run_id: str,
        *,
        after_event_id: str | None = ...,
        after_sequence: int | None = ...,
        limit: int = ...,
    ) -> EventBatch: ...


class Investigations(Protocol):
    async def start(
        self,
        principal: Principal,
        *,
        session_id: str,
        text: str,
        submission_key: str,
    ) -> RunHandle: ...

    async def submit(
        self,
        principal: Principal,
        *,
        session_id: str,
        text: str,
        submission_key: str,
        mode: SubmitMode = ...,
    ) -> InputReceipt: ...

    async def steer(
        self, principal: Principal, *, run_id: str, text: str, submission_key: str
    ) -> InputReceipt: ...

    async def answer(
        self,
        principal: Principal,
        *,
        run_id: str,
        question_id: str,
        text: str,
        submission_key: str,
    ) -> InputReceipt: ...

    async def cancel(self, principal: Principal, *, run_id: str) -> Run: ...


class Reports(Protocol):
    async def list_reports(
        self,
        principal: Principal,
        *,
        session_id: str | None = ...,
        limit: int = ...,
        offset: int = ...,
    ) -> tuple[ReportListing, ...]: ...

    async def versions(
        self, principal: Principal, report_id: str
    ) -> tuple[ReportListing, ...]: ...

    async def read(
        self, principal: Principal, report_id: str, version: int | None = ...
    ) -> ReportDocument: ...

    async def export(
        self, principal: Principal, report_id: str, version: int | None = ...
    ) -> ExportedReport: ...

    async def search(
        self,
        principal: Principal,
        query: str,
        *,
        session_id: str | None = ...,
        limit: int = ...,
    ) -> ReportSearchResult: ...


class Deletions(Protocol):
    async def list_pending(
        self, principal: Principal
    ) -> tuple[DeletionPreview, ...]: ...

    async def preview(
        self, principal: Principal, proposal_id: str
    ) -> DeletionPreview: ...

    async def confirm(
        self, principal: Principal, proposal_id: str
    ) -> DeletionResult: ...

    async def cancel(
        self, principal: Principal, proposal_id: str
    ) -> DeletionPreview: ...


class Personas(Protocol):
    async def current(self, principal: Principal) -> PersonaVersion | None: ...

    async def get(self, principal: Principal, version_id: str) -> PersonaVersion: ...

    async def history(
        self, principal: Principal, *, limit: int = ...
    ) -> PersonaHistory: ...

    async def create_draft(
        self, principal: Principal, content: str, *, idempotency_key: str
    ) -> PersonaVersion: ...

    async def update_draft(
        self,
        principal: Principal,
        draft_id: str,
        content: str,
        *,
        expected_revision: int,
    ) -> PersonaVersion: ...

    async def discard_draft(
        self, principal: Principal, draft_id: str
    ) -> PersonaVersion: ...

    async def preview(self, principal: Principal, draft_id: str) -> PersonaPreview: ...

    async def publish(
        self, principal: Principal, draft_id: str, *, expected_current: str | None
    ) -> Publication: ...

    async def rollback(
        self, principal: Principal, version_id: str, *, expected_current: str | None
    ) -> Publication: ...


@dataclass(frozen=True)
class HttpServices:
    authenticator: Authenticates
    conversations: Conversations
    investigations: Investigations
    reports: Reports
    deletions: Deletions
    # Optional so a deployment (or a test) without persona management still
    # serves everything else; its routes then answer 503.
    persona: Personas | None = None


@dataclass(frozen=True)
class StreamSettings:
    """SSE timing. Clients reconnect with ``Last-Event-ID`` after a close."""

    poll_seconds: float = 0.5
    heartbeat_seconds: float = 15.0
    # A connection is closed after this long; the client resumes from its cursor.
    max_seconds: float = 900.0
    # After the run ended without a terminal event yet, wait this long for it.
    terminal_grace_seconds: float = 2.0
    # Suggested client reconnection delay (SSE ``retry:``).
    retry_millis: int = 2000


ServicesProvider = Callable[[], AbstractAsyncContextManager[HttpServices]]
