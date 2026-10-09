"""ConversationService over the in-memory context world (real resolver, guard
and output gate): ownership, fresh authority at release and gap-free events."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.persistence import RecordNotFound
from retail_analytics.application.contracts.progress import (
    EventKind,
    InputRequest,
    ProgressEvent,
    ProgressUpdate,
)
from retail_analytics.application.conversations import (
    WITHHELD_SUMMARY,
    ConversationService,
    session_id_for,
)
from retail_analytics.domain.conversation import Message, MessageRole, Session
from retail_analytics.domain.investigations import (
    ClarificationQuestion,
    QuestionStatus,
)
from retail_analytics.domain.runs import Run, RunStatus
from tests.unit.context.support import A, B, World
from tests.unit.privacy.support import EXEC_A

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 10, 8, 12, tzinfo=UTC)


def event(run: Run, sequence: int, summary: str, **kw: object) -> ProgressEvent:
    update = ProgressUpdate(
        correlation=Correlation(session_id=run.session_id, run_id=run.run_id),
        kind=kw.pop("kind", EventKind.ANALYSIS_PROGRESS),  # type: ignore[arg-type]
        summary=summary,
        **kw,  # type: ignore[arg-type]
    )
    return ProgressEvent.stamp(
        update, event_id=f"e{sequence}", sequence=sequence, occurred_at=T0
    )


@dataclass
class Events:
    stored: dict[str, list[ProgressEvent]] = field(default_factory=dict)
    # Sequences hidden from readers (simulates an uncommitted append).
    hidden: set[int] = field(default_factory=set)

    async def append(self, update: ProgressUpdate) -> ProgressEvent:
        raise NotImplementedError

    async def publish(self, update: ProgressUpdate) -> None:
        raise NotImplementedError

    async def replay(
        self, run_id: str, *, after_event_id: str | None = None, limit: int = 500
    ) -> Sequence[ProgressEvent]:
        events = self.stored.get(run_id, [])
        after = 0
        if after_event_id is not None:
            found = [e.sequence for e in events if e.event_id == after_event_id]
            if not found:
                raise RecordNotFound("run event", after_event_id)
            after = found[0]
        visible = [
            e for e in events if e.sequence > after and e.sequence not in self.hidden
        ]
        return visible[:limit]


@dataclass
class Inputs:
    questions: dict[str, ClarificationQuestion] = field(default_factory=dict)

    async def open_question(self, run_id: str) -> ClarificationQuestion | None:
        return self.questions.get(run_id)


@dataclass
class Reader:
    world: World
    answers: dict[str, Message] = field(default_factory=dict)

    async def sessions_for(
        self, executive_id: str, *, limit: int, offset: int
    ) -> Sequence[Session]:
        own = [
            s
            for s in self.world.records.sessions.values()
            if s.executive_id == executive_id
        ]
        return own[offset : offset + limit]

    async def runs_in_session(self, session_id: str, *, limit: int) -> Sequence[Run]:
        return [
            r for r in self.world.records.runs.values() if r.session_id == session_id
        ][:limit]

    async def message(self, message_id: str) -> Message | None:
        found = [m for m in self.world.history.messages if m.message_id == message_id]
        return found[0] if found else None

    async def run_answer(self, run_id: str) -> Message | None:
        return self.answers.get(run_id)


@dataclass
class Sessions:
    world: World

    async def create_session(self, session_id: str, executive_id: str) -> Session:
        existing = self.world.records.sessions.get(session_id)
        if existing is None:
            existing = Session(session_id, executive_id, T0, T0)
            self.world.records.sessions[session_id] = existing
        return existing


class Env:
    def __init__(self) -> None:
        self.world = World()
        self.events = Events()
        self.inputs = Inputs()
        self.reader = Reader(self.world)
        self.service = ConversationService(
            resolver=self.world.resolver,
            guard=self.world.guard,
            sessions=Sessions(self.world),  # type: ignore[arg-type]
            runs=self.world.records,  # type: ignore[arg-type]
            inputs=self.inputs,  # type: ignore[arg-type]
            events=self.events,
            reader=self.reader,
            gate=self.world.gate,
        )
        self.run = self.world.records.runs["r-a"]

    def add(self, *summaries: str) -> None:
        stored = self.events.stored.setdefault(self.run.run_id, [])
        for summary in summaries:
            stored.append(event(self.run, len(stored) + 1, summary))


@pytest.fixture
def env() -> Env:
    return Env()


async def test_events_replay_after_cursor_in_order(env: Env) -> None:
    env.add("one", "two", "three")
    first = await env.service.events(A, "r-a")
    assert [e.sequence for e in first.events] == [1, 2, 3]
    assert first.caught_up
    later = await env.service.events(A, "r-a", after_event_id="e1", after_sequence=1)
    assert [e.summary for e in later.events] == ["two", "three"]


async def test_other_executive_cannot_read_events_or_view(env: Env) -> None:
    env.add("one")
    with pytest.raises(AccessDenied):
        await env.service.events(B, "r-a")
    with pytest.raises(AccessDenied):
        await env.service.run_view(B, "r-a")
    with pytest.raises(AccessDenied):
        await env.service.events(A, "missing-run")


async def test_foreign_cursor_is_rejected(env: Env) -> None:
    env.add("one")
    with pytest.raises(RecordNotFound):
        await env.service.events(A, "r-a", after_event_id="not-an-event")


async def test_batch_stops_before_a_sequence_gap(env: Env) -> None:
    env.add("one", "two", "three")
    env.events.hidden = {2}
    batch = await env.service.events(A, "r-a")
    assert [e.sequence for e in batch.events] == [1]
    assert not batch.caught_up
    again = await env.service.events(A, "r-a", after_event_id="e1", after_sequence=1)
    assert again.events == ()
    env.events.hidden = set()
    filled = await env.service.events(A, "r-a", after_event_id="e1", after_sequence=1)
    assert [e.sequence for e in filled.events] == [2, 3]


async def test_every_event_passes_the_output_gate(env: Env) -> None:
    env.add(
        "Contacted alice.smith@example.com about the order.",
        "Filtering on _policy_product_ids now.",
    )
    batch = await env.service.events(A, "r-a")
    masked, withheld = batch.events
    assert "alice.smith@example.com" not in masked.summary
    assert "[withheld]" in masked.summary
    # Refused summaries are replaced, never dropped: the sequence stays whole.
    assert withheld.summary == WITHHELD_SUMMARY
    assert withheld.sequence == 2


async def test_question_text_in_events_is_released_too(env: Env) -> None:
    stored = env.events.stored.setdefault("r-a", [])
    stored.append(
        event(
            env.run,
            1,
            "Waiting for your answer.",
            kind=EventKind.INPUT_REQUIRED,
            input_request=InputRequest(
                question_id="q1", question="Should I email bob@example.com?"
            ),
        )
    )
    (released,) = (await env.service.events(A, "r-a")).events
    assert released.input_request is not None
    assert "bob@example.com" not in released.input_request.question


async def test_authority_is_rechecked_on_every_batch(env: Env) -> None:
    env.add("one")
    await env.service.events(A, "r-a")
    current = env.world.directory.by_id[EXEC_A]
    env.world.directory.by_id[EXEC_A] = replace(current, active=False)
    with pytest.raises(AccessDenied):
        await env.service.events(A, "r-a", after_event_id="e1", after_sequence=1)


async def test_run_view_releases_question_and_answer(env: Env) -> None:
    message = env.world.say(MessageRole.ASSISTANT, "Ask carol@example.com?", "r-a")
    env.inputs.questions["r-a"] = ClarificationQuestion(
        question_id="q1",
        run_id="r-a",
        message_id=message.message_id,
        status=QuestionStatus.OPEN,
        asked_at=T0,
    )
    view = await env.service.run_view(A, "r-a")
    assert view.question is not None and view.question.question_id == "q1"
    assert "carol@example.com" not in view.question.text.text
    assert view.answer is None

    env.inputs.questions.clear()
    env.world.records.runs["r-a"] = replace(env.run, status=RunStatus.COMPLETED)
    env.reader.answers["r-a"] = env.world.say(
        MessageRole.ASSISTANT, "Revenue rose; see _policy_ref_inner.", "r-a"
    )
    done = await env.service.run_view(A, "r-a")
    assert done.answer is not None and done.answer.withheld
    assert "_policy_" not in done.answer.text


async def test_sessions_are_owner_scoped_and_idempotent(env: Env) -> None:
    first = await env.service.open_session(A, "key-1")
    again = await env.service.open_session(A, "key-1")
    assert first == again
    assert first.session_id == session_id_for(EXEC_A, "key-1")
    assert session_id_for("someone-else", "key-1") != first.session_id
    listed = await env.service.list_sessions(A)
    assert first in listed
    assert all(s.executive_id == EXEC_A for s in listed)
    with pytest.raises(AccessDenied):
        await env.service.session(B, first.session_id)
    overview = await env.service.session(A, "s-a")
    assert [r.run_id for r in overview.runs] == ["r-a"]
