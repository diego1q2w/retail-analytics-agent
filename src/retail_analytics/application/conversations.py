"""Sessions, run views and progress delivery for an authenticated client.

Everything is owner-scoped: sessions and runs of another executive raise the
same ``AccessDenied`` as missing ones. Starting, steering, answering and
cancelling go through ``InvestigationControl``; this module only opens
sessions and reads what a client may see.

Every piece of generated text leaving through here is released by the output
privacy gate under a policy built *after* the text was read, so a revoked or
narrowed authority applies to replayed events, open questions and answers
alike. A section the gate refuses is replaced by its safe explanation rather
than dropped, so event sequences stay gap-free.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
    OwnershipGuard,
)
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.conversations import (
    EventBatch,
    OpenQuestion,
    ReleasedText,
    RunView,
    SessionOverview,
)
from retail_analytics.application.contracts.progress import ProgressEvent
from retail_analytics.application.output_privacy import (
    DisclosurePolicy,
    OutputDestination,
    OutputPrivacyGate,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.application.ports.conversations import ConversationReader
from retail_analytics.application.ports.investigations import InvestigationInputs
from retail_analytics.application.ports.persistence import (
    RunEventStore,
    RunRepository,
    SessionRepository,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.conversation import Session

MAX_PAGE = 100
MAX_EVENT_BATCH = 500
WITHHELD_SUMMARY = "Progress update withheld."
WITHHELD_QUESTION = "A question is waiting, but its text was withheld."
_SUMMARY_CHARS = 280
_QUESTION_CHARS = 1000


def session_id_for(executive_id: str, submission_key: str) -> str:
    """Stable session ID per (executive, client key): retries open one session."""
    digest = hashlib.sha256(f"{executive_id}\x1f{submission_key}".encode())
    return "ses_" + digest.hexdigest()[:32]


def _page(limit: int) -> int:
    return max(1, min(MAX_PAGE, limit))


class ConversationService:
    def __init__(
        self,
        *,
        resolver: AccessResolver,
        guard: OwnershipGuard,
        sessions: SessionRepository,
        runs: RunRepository,
        inputs: InvestigationInputs,
        events: RunEventStore,
        reader: ConversationReader,
        gate: OutputPrivacyGate,
    ) -> None:
        self._resolver = resolver
        self._guard = guard
        self._sessions = sessions
        self._runs = runs
        self._inputs = inputs
        self._events = events
        self._reader = reader
        self._gate = gate

    # --- sessions -------------------------------------------------------------

    async def open_session(self, principal: Principal, submission_key: str) -> Session:
        """Open (or, for a repeated key, return) a session of the caller."""
        await self._resolver.require_permission(principal, Permission.ANALYSIS_READ)
        return await self._sessions.create_session(
            session_id_for(principal.executive_id, submission_key),
            principal.executive_id,
        )

    async def list_sessions(
        self, principal: Principal, *, limit: int = 20, offset: int = 0
    ) -> tuple[Session, ...]:
        await self._resolver.current_access(principal)
        rows = await self._reader.sessions_for(
            principal.executive_id, limit=_page(limit), offset=max(0, offset)
        )
        return tuple(rows)

    async def session(
        self, principal: Principal, session_id: str, *, run_limit: int = 20
    ) -> SessionOverview:
        await self._resolver.current_access(principal)
        session = await self._guard.session(principal.executive_id, session_id)
        runs = await self._reader.runs_in_session(session_id, limit=_page(run_limit))
        # A run is the caller's only if they requested it (sessions are
        # single-owner today; this keeps the rule explicit).
        return SessionOverview(
            session,
            tuple(r for r in runs if r.requested_by == principal.executive_id),
        )

    # --- runs -----------------------------------------------------------------

    async def run_view(self, principal: Principal, run_id: str) -> RunView:
        """Status, open question and (once terminal) the released answer."""
        await self._resolver.context_for_run(principal, run_id)
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AccessDenied("run", run_id)
        question = await self._inputs.open_question(run_id)
        question_text = None
        if question is not None:
            message = await self._reader.message(question.message_id)
            question_text = None if message is None else message.content
        answer = (
            await self._reader.run_answer(run_id) if run.status.is_terminal else None
        )
        if question_text is None and answer is None:
            return RunView(run, None, None)
        policy = await self._gate.policy_for_run(principal, run_id, trace_id=run_id)
        open_question = None
        if question is not None:
            open_question = OpenQuestion(
                question.question_id,
                self._release(
                    policy,
                    question_text or "",
                    OutputDestination.PROGRESS,
                    "question",
                    limit=_QUESTION_CHARS,
                    withheld=WITHHELD_QUESTION,
                ),
            )
        released_answer = None
        if answer is not None:
            released_answer = self._release(
                policy, answer.content, OutputDestination.DISPLAY, "answer"
            )
        return RunView(run, open_question, released_answer)

    async def events(
        self,
        principal: Principal,
        run_id: str,
        *,
        after_event_id: str | None = None,
        after_sequence: int | None = None,
        limit: int = MAX_EVENT_BATCH,
    ) -> EventBatch:
        """Ordered events after the cursor, released for display now.

        ``after_sequence`` is the sequence of ``after_event_id`` when the
        caller knows it (a stream does after its first event); the batch then
        stops before any gap. Raises ``AccessDenied`` for a run that is not
        the caller's and ``RecordNotFound`` for a cursor not of this run.
        """
        limit = max(1, min(MAX_EVENT_BATCH, limit))
        await self._resolver.context_for_run(principal, run_id)
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AccessDenied("run", run_id)
        stored = await self._events.replay(
            run_id, after_event_id=after_event_id, limit=limit
        )
        if after_event_id is None:
            expected: int | None = 1
        else:
            expected = None if after_sequence is None else after_sequence + 1
        contiguous = _contiguous(stored, expected)
        if not contiguous:
            return EventBatch((), run.status, len(stored) < limit)
        # Authority is resolved again after reading: release reflects now.
        policy = await self._gate.policy_for_run(principal, run_id, trace_id=run_id)
        released = tuple(self._release_event(policy, e) for e in contiguous)
        return EventBatch(
            released,
            run.status,
            len(stored) < limit and len(contiguous) == len(stored),
        )

    # --- release --------------------------------------------------------------

    def _release_event(
        self, policy: DisclosurePolicy, event: ProgressEvent
    ) -> ProgressEvent:
        summary = self._release(
            policy,
            event.summary,
            OutputDestination.PROGRESS,
            "summary",
            limit=_SUMMARY_CHARS,
            withheld=WITHHELD_SUMMARY,
        ).text
        update: dict[str, object] = {"summary": summary}
        if event.input_request is not None:
            question = self._release(
                policy,
                event.input_request.question,
                OutputDestination.PROGRESS,
                "question",
                limit=_QUESTION_CHARS,
                withheld=WITHHELD_QUESTION,
            ).text
            update["input_request"] = event.input_request.model_copy(
                update={"question": question}
            )
        return event.model_copy(update=update)

    def _release(
        self,
        policy: DisclosurePolicy,
        text: str,
        destination: OutputDestination,
        name: str,
        *,
        limit: int | None = None,
        withheld: str | None = None,
    ) -> ReleasedText:
        try:
            released = self._gate.release(
                policy, OutputSection(name, text), destination
            ).text
        except OutputWithheld as refused:
            return ReleasedText(withheld or refused.message, withheld=True)
        if limit is not None and len(released) > limit:
            released = released[: limit - 1] + "…"
        return ReleasedText(released)


def _contiguous(
    events: Sequence[ProgressEvent], expected_first: int | None
) -> tuple[ProgressEvent, ...]:
    """The prefix of ``events`` without a sequence gap."""
    kept: list[ProgressEvent] = []
    expected = expected_first
    for event in events:
        if expected is not None and event.sequence != expected:
            break
        kept.append(event)
        expected = event.sequence + 1
    return tuple(kept)
