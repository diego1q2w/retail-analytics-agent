"""Versioned HTTP request/response contracts (served as OpenAPI at /openapi.json).

Requests are strict (unknown fields are rejected), so a client cannot send
identity, entitlements or approvals in a body: identity comes only from the
bearer token. See docs/http-api.md for the SSE event stream.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from retail_analytics.application.contracts import Identifier
from retail_analytics.application.contracts.conversations import (
    ReleasedText,
    RunView,
)
from retail_analytics.application.contracts.report_deletion import (
    DeletionPreview,
    DeletionResult,
)
from retail_analytics.application.contracts.reports import (
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
from retail_analytics.domain.investigations import MAX_INPUT_CHARS
from retail_analytics.domain.report_definitions import DefinitionNotice
from retail_analytics.domain.runs import Run

API_VERSION = 1

MessageText = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_INPUT_CHARS)
]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- errors -------------------------------------------------------------------


class ErrorBody(_Response):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(_Response):
    error: ErrorBody


# --- sessions and runs ----------------------------------------------------------


class OpenSessionRequest(_Request):
    # Client-chosen key: repeating it returns the same session.
    submission_key: Identifier


class SessionOut(_Response):
    session_id: str
    created_at: datetime
    last_activity_at: datetime

    @classmethod
    def of(cls, session: Session) -> SessionOut:
        return cls(
            session_id=session.session_id,
            created_at=session.created_at,
            last_activity_at=session.last_activity_at,
        )


class RunSummaryOut(_Response):
    run_id: str
    session_id: str
    status: str
    active: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, run: Run) -> RunSummaryOut:
        return cls(
            run_id=run.run_id,
            session_id=run.session_id,
            status=run.status.value,
            active=run.status.is_active,
            created_at=run.created_at,
            updated_at=run.updated_at,
            completed_at=run.completed_at,
        )


class SessionDetailOut(SessionOut):
    runs: list[RunSummaryOut]


class SessionListOut(_Response):
    sessions: list[SessionOut]


class StartRunRequest(_Request):
    text: MessageText
    submission_key: Identifier


class MessageRequest(_Request):
    text: MessageText
    submission_key: Identifier
    # steer (default): refine the active run or start one; queue: a separate
    # request that waits for the active run.
    mode: SubmitMode = SubmitMode.STEER


class SteerRequest(_Request):
    text: MessageText
    submission_key: Identifier


class AnswerRequest(_Request):
    question_id: Identifier
    text: MessageText
    submission_key: Identifier


class RunHandleOut(_Response):
    run_id: str
    status: str
    # False when the submission key matched an earlier request.
    created: bool

    @classmethod
    def of(cls, handle: RunHandle) -> RunHandleOut:
        return cls(
            run_id=handle.run_id, status=handle.status.value, created=handle.created
        )


class InputReceiptOut(_Response):
    input_id: str
    kind: str
    # None while a queued request waits behind the active run.
    run_id: str | None

    @classmethod
    def of(cls, receipt: InputReceipt) -> InputReceiptOut:
        return cls(
            input_id=receipt.input_id, kind=receipt.kind.value, run_id=receipt.run_id
        )


class ReleasedTextOut(_Response):
    text: str
    # True when the output gate withheld the text; ``text`` then explains.
    withheld: bool

    @classmethod
    def of(cls, released: ReleasedText) -> ReleasedTextOut:
        return cls(text=released.text, withheld=released.withheld)


class QuestionOut(_Response):
    question_id: str
    text: ReleasedTextOut


class RunDetailOut(RunSummaryOut):
    question: QuestionOut | None
    answer: ReleasedTextOut | None

    @classmethod
    def of_view(cls, view: RunView) -> RunDetailOut:
        summary = RunSummaryOut.of(view.run)
        return cls(
            **summary.model_dump(),
            question=None
            if view.question is None
            else QuestionOut(
                question_id=view.question.question_id,
                text=ReleasedTextOut.of(view.question.text),
            ),
            answer=None if view.answer is None else ReleasedTextOut.of(view.answer),
        )


class CancelOut(_Response):
    run: RunSummaryOut


# --- reports ------------------------------------------------------------------


class ReportListingOut(_Response):
    report_id: str
    version: int
    # None when the owner's product access changed since it was saved.
    title: str | None
    created_at: datetime
    session_id: str | None
    evidence_count: int
    access: str

    @classmethod
    def of(cls, listing: ReportListing) -> ReportListingOut:
        return cls(
            report_id=listing.report_id,
            version=listing.version,
            title=listing.title,
            created_at=listing.created_at,
            session_id=listing.session_id,
            evidence_count=listing.evidence_count,
            access=listing.access.value,
        )


class ReportListOut(_Response):
    reports: list[ReportListingOut]


class ReportMatchOut(_Response):
    report: ReportListingOut
    matched_in: str
    snippet: str


class ReportSearchOut(_Response):
    matches: list[ReportMatchOut]
    scanned: int
    withheld: int
    scan_limited: bool

    @classmethod
    def of(cls, result: ReportSearchResult) -> ReportSearchOut:
        return cls(
            matches=[
                ReportMatchOut(
                    report=ReportListingOut.of(m.listing),
                    matched_in=m.matched_in,
                    snippet=m.snippet,
                )
                for m in result.matches
            ],
            scanned=result.scanned,
            withheld=result.withheld,
            scan_limited=result.scan_limited,
        )


class CitedEvidenceOut(_Response):
    evidence_id: str
    kind: str
    computed_at: datetime
    period_start: date | None
    period_end: date | None
    truncated: bool
    columns: list[str]
    rows: list[list[str]]


# Export response header carrying the report's definition notices as a JSON
# list of ``DefinitionNoticeOut`` (ASCII), because the body is the file itself.
NOTICES_HEADER = "X-Report-Definition-Notices"


class DefinitionNoticeOut(_Response):
    """Shown with a report (never part of it): a definition it used differs
    from the reader's current one, or its definitions were not recorded."""

    kind: str
    message: str
    subject: str | None
    report_definition: str | None
    current_definition: str | None
    recalculation_required: bool

    @classmethod
    def of(cls, notice: DefinitionNotice) -> DefinitionNoticeOut:
        return cls(
            kind=notice.kind.value,
            message=notice.message,
            subject=notice.subject,
            report_definition=notice.report_definition,
            current_definition=notice.current_definition,
            recalculation_required=notice.recalculation_required,
        )


class ReportDocumentOut(_Response):
    report_id: str
    version: int
    title: str
    created_at: datetime
    session_id: str | None
    run_id: str | None
    markdown: str
    evidence: list[CitedEvidenceOut]
    definition_notices: list[DefinitionNoticeOut]

    @classmethod
    def of(cls, document: ReportDocument) -> ReportDocumentOut:
        v = document.version
        return cls(
            report_id=v.report_id,
            version=v.version,
            title=v.title,
            created_at=v.created_at,
            session_id=v.session_id,
            run_id=v.run_id,
            markdown=document.markdown,
            evidence=[
                CitedEvidenceOut(
                    evidence_id=e.evidence_id,
                    kind=e.kind,
                    computed_at=e.computed_at,
                    period_start=e.period_start,
                    period_end=e.period_end,
                    truncated=e.truncated,
                    columns=list(e.columns),
                    rows=[list(r) for r in e.rows],
                )
                for e in document.evidence
            ],
            definition_notices=[DefinitionNoticeOut.of(n) for n in document.notices],
        )


# --- deletion -----------------------------------------------------------------


class ConfirmDeletionRequest(_Request):
    # The user's explicit approval; nothing else (and no model output) confirms.
    confirm: Literal[True]


class DeletionItemOut(_Response):
    report_id: str
    version: int
    title: str | None
    created_at: datetime


class DeletionPreviewOut(_Response):
    proposal_id: str
    status: str
    expires_at: datetime
    count: int
    items: list[DeletionItemOut]

    @classmethod
    def of(cls, preview: DeletionPreview) -> DeletionPreviewOut:
        return cls(
            proposal_id=preview.proposal_id,
            status=preview.status.value,
            expires_at=preview.expires_at,
            count=preview.count,
            items=[
                DeletionItemOut(
                    report_id=i.report_id,
                    version=i.version,
                    title=i.title,
                    created_at=i.created_at,
                )
                for i in preview.items
            ],
        )


class DeletionProposalListOut(_Response):
    proposals: list[DeletionPreviewOut]

    @classmethod
    def of(cls, previews: tuple[DeletionPreview, ...]) -> DeletionProposalListOut:
        return cls(proposals=[DeletionPreviewOut.of(p) for p in previews])


class DeletionResultOut(_Response):
    proposal_id: str
    report_ids: list[str]
    deleted_at: datetime
    # An operator can restore them until then (CLI, not this API).
    recoverable_until: datetime

    @classmethod
    def of(cls, result: DeletionResult) -> DeletionResultOut:
        return cls(
            proposal_id=result.proposal_id,
            report_ids=list(result.report_ids),
            deleted_at=result.deleted_at,
            recoverable_until=result.recoverable_until,
        )
