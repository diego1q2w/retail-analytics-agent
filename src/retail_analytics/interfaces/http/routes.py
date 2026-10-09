"""Version 1 HTTP routes. Each one authenticates, calls one use case and maps
its result; ownership, permissions and output release live in the
application layer."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter, ValidationError

from retail_analytics.application.contracts import Identifier
from retail_analytics.interfaces.http.dependencies import CurrentPrincipal, Services
from retail_analytics.interfaces.http.errors import ApiError
from retail_analytics.interfaces.http.persona_routes import build_persona_router
from retail_analytics.interfaces.http.schemas import (
    AnswerRequest,
    CancelOut,
    ConfirmDeletionRequest,
    DeletionPreviewOut,
    DeletionProposalListOut,
    DeletionResultOut,
    ErrorResponse,
    InputReceiptOut,
    MessageRequest,
    OpenSessionRequest,
    ReportDocumentOut,
    ReportListingOut,
    ReportListOut,
    ReportSearchOut,
    RunDetailOut,
    RunHandleOut,
    RunSummaryOut,
    SessionDetailOut,
    SessionListOut,
    SessionOut,
    StartRunRequest,
    SteerRequest,
)
from retail_analytics.interfaces.http.services import StreamSettings
from retail_analytics.interfaces.http.streaming import event_stream

_ERRORS: dict[int | str, dict[str, object]] = {
    status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 410, 422, 503)
}
_IDENTIFIER: TypeAdapter[str] = TypeAdapter(Identifier)

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100_000)]
Version = Annotated[int | None, Query(ge=1)]


def _identifier(value: str, what: str) -> str:
    try:
        return _IDENTIFIER.validate_python(value)
    except ValidationError:
        raise ApiError(400, "invalid_cursor", f"{what} is malformed.") from None


def build_router(stream: StreamSettings) -> APIRouter:
    router = APIRouter(prefix="/v1", responses=_ERRORS)

    # --- sessions -------------------------------------------------------------

    @router.post("/sessions", status_code=201, tags=["sessions"])
    async def open_session(
        body: OpenSessionRequest, principal: CurrentPrincipal, s: Services
    ) -> SessionOut:
        """Open a session (a repeated ``submission_key`` returns the same one)."""
        session = await s.conversations.open_session(principal, body.submission_key)
        return SessionOut.of(session)

    @router.get("/sessions", tags=["sessions"])
    async def list_sessions(
        principal: CurrentPrincipal,
        s: Services,
        limit: Limit = 20,
        offset: Offset = 0,
    ) -> SessionListOut:
        sessions = await s.conversations.list_sessions(
            principal, limit=limit, offset=offset
        )
        return SessionListOut(sessions=[SessionOut.of(x) for x in sessions])

    @router.get("/sessions/{session_id}", tags=["sessions"])
    async def get_session(
        session_id: str, principal: CurrentPrincipal, s: Services, limit: Limit = 20
    ) -> SessionDetailOut:
        overview = await s.conversations.session(principal, session_id, run_limit=limit)
        return SessionDetailOut(
            **SessionOut.of(overview.session).model_dump(),
            runs=[RunSummaryOut.of(r) for r in overview.runs],
        )

    @router.post("/sessions/{session_id}/runs", status_code=202, tags=["runs"])
    async def start_run(
        session_id: str,
        body: StartRunRequest,
        principal: CurrentPrincipal,
        s: Services,
    ) -> RunHandleOut:
        """Start an investigation; 409 ``active_run_exists`` if one is active."""
        handle = await s.investigations.start(
            principal,
            session_id=session_id,
            text=body.text,
            submission_key=body.submission_key,
        )
        return RunHandleOut.of(handle)

    @router.post("/sessions/{session_id}/messages", status_code=202, tags=["runs"])
    async def send_message(
        session_id: str,
        body: MessageRequest,
        principal: CurrentPrincipal,
        s: Services,
    ) -> InputReceiptOut:
        """A message: steers the active run (or starts one), or with
        ``mode=queue`` waits as a separate request behind it."""
        receipt = await s.investigations.submit(
            principal,
            session_id=session_id,
            text=body.text,
            submission_key=body.submission_key,
            mode=body.mode,
        )
        return InputReceiptOut.of(receipt)

    # --- runs -----------------------------------------------------------------

    @router.get("/runs/{run_id}", tags=["runs"])
    async def get_run(
        run_id: str, principal: CurrentPrincipal, s: Services
    ) -> RunDetailOut:
        """Status, the open question and (once ended) the released answer."""
        return RunDetailOut.of_view(await s.conversations.run_view(principal, run_id))

    @router.post("/runs/{run_id}/steer", status_code=202, tags=["runs"])
    async def steer_run(
        run_id: str, body: SteerRequest, principal: CurrentPrincipal, s: Services
    ) -> InputReceiptOut:
        receipt = await s.investigations.steer(
            principal, run_id=run_id, text=body.text, submission_key=body.submission_key
        )
        return InputReceiptOut.of(receipt)

    @router.post("/runs/{run_id}/answers", status_code=202, tags=["runs"])
    async def answer_question(
        run_id: str, body: AnswerRequest, principal: CurrentPrincipal, s: Services
    ) -> InputReceiptOut:
        """Answer the run's open clarification question."""
        receipt = await s.investigations.answer(
            principal,
            run_id=run_id,
            question_id=body.question_id,
            text=body.text,
            submission_key=body.submission_key,
        )
        return InputReceiptOut.of(receipt)

    @router.post("/runs/{run_id}/cancel", status_code=202, tags=["runs"])
    async def cancel_run(
        run_id: str, principal: CurrentPrincipal, s: Services
    ) -> CancelOut:
        """Stop new work; the run reports ``cancelled`` once effects settle."""
        run = await s.investigations.cancel(principal, run_id=run_id)
        return CancelOut(run=RunSummaryOut.of(run))

    @router.get(
        "/runs/{run_id}/events",
        tags=["runs"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def run_events(
        run_id: str,
        request: Request,
        principal: CurrentPrincipal,
        s: Services,
        last_event_id: Annotated[str | None, Header()] = None,
        after: Annotated[str | None, Query(max_length=128)] = None,
    ) -> StreamingResponse:
        """Server-Sent Events of the run's progress after ``Last-Event-ID``
        (header, or ``after`` query parameter)."""
        cursor = last_event_id or after
        if cursor is not None:
            cursor = _identifier(cursor, "Last-Event-ID")
        # Authorization and cursor errors are answered before streaming starts.
        first = await s.conversations.events(principal, run_id, after_event_id=cursor)
        return StreamingResponse(
            event_stream(
                conversations=s.conversations,
                principal=principal,
                run_id=run_id,
                first=first,
                after_event_id=cursor,
                settings=stream,
                disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --- reports --------------------------------------------------------------

    @router.get("/reports", tags=["reports"])
    async def list_reports(
        principal: CurrentPrincipal,
        s: Services,
        session_id: str | None = None,
        limit: Limit = 20,
        offset: Offset = 0,
    ) -> ReportListOut:
        listings = await s.reports.list_reports(
            principal, session_id=session_id, limit=limit, offset=offset
        )
        return ReportListOut(reports=[ReportListingOut.of(x) for x in listings])

    @router.get("/reports/search", tags=["reports"])
    async def search_reports(
        principal: CurrentPrincipal,
        s: Services,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        session_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> ReportSearchOut:
        result = await s.reports.search(
            principal, q, session_id=session_id, limit=limit
        )
        return ReportSearchOut.of(result)

    @router.get("/reports/{report_id}", tags=["reports"])
    async def read_report(
        report_id: str,
        principal: CurrentPrincipal,
        s: Services,
        version: Version = None,
    ) -> ReportDocumentOut:
        return ReportDocumentOut.of(await s.reports.read(principal, report_id, version))

    @router.get("/reports/{report_id}/versions", tags=["reports"])
    async def report_versions(
        report_id: str, principal: CurrentPrincipal, s: Services
    ) -> ReportListOut:
        listings = await s.reports.versions(principal, report_id)
        return ReportListOut(reports=[ReportListingOut.of(x) for x in listings])

    @router.get(
        "/reports/{report_id}/export",
        tags=["reports"],
        response_class=Response,
        responses={200: {"content": {"text/markdown": {}}}},
    )
    async def export_report(
        report_id: str,
        principal: CurrentPrincipal,
        s: Services,
        version: Version = None,
    ) -> Response:
        """The report with its evidence appendix as one Markdown file."""
        exported = await s.reports.export(principal, report_id, version)
        return Response(
            exported.content,
            media_type=exported.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{exported.filename}"',
                "Cache-Control": "no-store",
            },
        )

    # --- deletion proposals ---------------------------------------------------
    # The model can only propose; these are the user's own decisions. There is
    # no restore route: recovery is an operator CLI action.

    @router.get("/deletion-proposals", tags=["deletion"])
    async def list_deletions(
        principal: CurrentPrincipal,
        s: Services,
        status: Annotated[Literal["pending"], Query()] = "pending",
    ) -> DeletionProposalListOut:
        """Your own pending, unexpired proposals, newest first."""
        del status  # pending is the only listing there is
        return DeletionProposalListOut.of(await s.deletions.list_pending(principal))

    @router.get("/deletion-proposals/{proposal_id}", tags=["deletion"])
    async def preview_deletion(
        proposal_id: str, principal: CurrentPrincipal, s: Services
    ) -> DeletionPreviewOut:
        return DeletionPreviewOut.of(await s.deletions.preview(principal, proposal_id))

    @router.post("/deletion-proposals/{proposal_id}/confirm", tags=["deletion"])
    async def confirm_deletion(
        proposal_id: str,
        body: ConfirmDeletionRequest,
        principal: CurrentPrincipal,
        s: Services,
    ) -> DeletionResultOut:
        """Delete exactly the proposed reports. Requires ``{"confirm": true}``."""
        del body  # its validation is the explicit approval
        return DeletionResultOut.of(await s.deletions.confirm(principal, proposal_id))

    @router.post("/deletion-proposals/{proposal_id}/cancel", tags=["deletion"])
    async def cancel_deletion(
        proposal_id: str, principal: CurrentPrincipal, s: Services
    ) -> DeletionPreviewOut:
        return DeletionPreviewOut.of(await s.deletions.cancel(principal, proposal_id))

    router.include_router(build_persona_router())
    return router
