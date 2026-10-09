"""Release and re-display recheck every evidence record linked to the run (G-1).

An answer resting on the run's evidence is withheld once that evidence is no
longer readable, even when it cites nothing and holds no recognisable figure;
the same answer is released while access is unchanged.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from retail_analytics.application.output_privacy import (
    ACCESS_CHANGED,
    DisclosurePolicy,
    OutputDestination,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.investigations import ClarificationQuestion, QuestionStatus
from retail_analytics.domain.runs import RunStatus
from tests.unit.context.support import BRAND_SQL, NARROW, A
from tests.unit.http.test_conversations import T0, Env
from tests.unit.privacy.support import EXEC_A

DERIVED = "Beta is your strongest brand, up 12% on 7 orders."
DISPLAY = OutputDestination.DISPLAY


@pytest.mark.asyncio
async def test_uncited_answer_is_released_then_withheld_after_revocation() -> None:
    env = Env()
    w = env.world
    run = w.new_run()
    await w.query(run, BRAND_SQL)
    (released,) = await w.gate.check(
        A, run, [OutputSection("answer", DERIVED)], DISPLAY
    )
    assert released.text == DERIVED

    w.set_products(EXEC_A, NARROW)
    for destination in OutputDestination:
        with pytest.raises(OutputWithheld) as caught:
            await w.gate.check(A, run, [OutputSection("answer", DERIVED)], destination)
        assert caught.value.reason == ACCESS_CHANGED
        assert not caught.value.correctable


@pytest.mark.asyncio
async def test_rule_is_scoped_to_the_run_being_released() -> None:
    env = Env()
    w = env.world
    earlier = w.new_run()
    await w.query(earlier, BRAND_SQL)
    w.set_products(EXEC_A, NARROW)
    later = w.new_run()
    await w.external(later, (("Alpha", 1),))
    # The later run rests only on evidence still readable: it is released.
    assert await w.gate.check(
        A, later, [OutputSection("answer", "Alpha leads.")], DISPLAY
    )


def test_linked_evidence_that_cannot_be_loaded_fails_closed() -> None:
    policy = DisclosurePolicy.from_standings(
        executive_id=EXEC_A,
        session_id="s-a",
        run_id="r1",
        authorization_version=1,
        standings=(),
        run_evidence=("ev-gone",),
    )
    assert policy.run_evidence_withdrawn


@pytest.mark.asyncio
async def test_redisplay_withholds_answer_and_question_after_revocation() -> None:
    env = Env()
    w = env.world
    await w.query("r-a", BRAND_SQL)
    question = w.say(MessageRole.ASSISTANT, "Should I split Beta by month?", "r-a")
    env.inputs.questions["r-a"] = ClarificationQuestion(
        question_id="q1",
        run_id="r-a",
        message_id=question.message_id,
        status=QuestionStatus.OPEN,
        asked_at=T0,
    )
    w.records.runs["r-a"] = replace(env.run, status=RunStatus.COMPLETED)
    env.reader.answers["r-a"] = w.say(MessageRole.ASSISTANT, DERIVED, "r-a")

    shown = await env.service.run_view(A, "r-a")
    assert shown.answer is not None and shown.answer.text == DERIVED
    assert shown.question is not None and not shown.question.text.withheld

    w.set_products(EXEC_A, NARROW)
    again = await env.service.run_view(A, "r-a")
    assert again.answer is not None and again.answer.withheld
    assert "strongest" not in again.answer.text and "12" not in again.answer.text
    assert "access or the privacy rules changed" in again.answer.text
    assert again.question is not None and again.question.text.withheld
    assert "Beta" not in again.question.text.text
