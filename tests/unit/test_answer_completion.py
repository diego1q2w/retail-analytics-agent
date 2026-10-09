"""A fully resolved refusal completes its run; remaining permitted work does not
(T26-F10).

``answer_completion`` decides from the model's structured output (permitted
work left, restrictions declined under) and restrictions the application
confirms: standing policy, or a refusal recorded in the run's operations.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from duckdb import DuckDBPyConnection as Connection

from retail_analytics.adapters.agent.investigator import AnswerOutput, proposal
from retail_analytics.application.answer_completion import (
    STANDING_RESTRICTIONS,
    answer_completion,
    recorded_restrictions,
)
from retail_analytics.application.contracts.investigations import Restriction
from retail_analytics.application.contracts.query_compiler import AnalysisQuery
from retail_analytics.application.investigation_policy import (
    render_investigation_policy,
)
from retail_analytics.application.query_execution import QueryAttempt, QueryFailed
from retail_analytics.domain.runs import RunStatus
from tests.unit.privacy.support import customer_database
from tests.unit.query_execution.test_scope_refusal import SNAPSHOT, Setup
from tests.unit.query_execution.test_service import PRINCIPAL

INDIVIDUAL = Restriction.INDIVIDUAL_DEMOGRAPHICS
SCOPE = Restriction.OUTSIDE_PERMITTED_SCOPE
NONE: frozenset[Restriction] = frozenset()


def test_refusal_only_request_is_complete() -> None:
    # "Which age band is our single biggest-spending customer?": declined,
    # group alternative given, nothing permitted left.
    done = answer_completion(complete=True, declined=[INDIVIDUAL], recorded=NONE)
    assert done.status is RunStatus.COMPLETED
    assert done.confirmed == (INDIVIDUAL,)
    assert done.unconfirmed == ()


def test_mixed_request_is_complete_when_permitted_work_is_done() -> None:
    done = answer_completion(complete=True, declined=[INDIVIDUAL], recorded=NONE)
    assert done.status is RunStatus.COMPLETED


def test_mixed_request_stays_partial_while_permitted_work_remains() -> None:
    left = answer_completion(complete=False, declined=[INDIVIDUAL], recorded=NONE)
    assert left.status is RunStatus.PARTIAL
    assert left.confirmed == (INDIVIDUAL,)


def test_without_a_refusal_the_model_flag_decides_as_before() -> None:
    assert (
        answer_completion(complete=True, declined=(), recorded=NONE).status
        is RunStatus.COMPLETED
    )
    assert (
        answer_completion(complete=False, declined=(), recorded=NONE).status
        is RunStatus.PARTIAL
    )


def test_scope_restriction_needs_a_refusal_recorded_in_the_run() -> None:
    # Scope depends on the executive's access: the claim alone is not enough.
    claimed = answer_completion(complete=True, declined=[SCOPE], recorded=NONE)
    assert claimed.status is RunStatus.PARTIAL
    assert claimed.unconfirmed == (SCOPE,)
    recorded = answer_completion(
        complete=True, declined=[SCOPE, SCOPE], recorded=frozenset({SCOPE})
    )
    assert recorded.status is RunStatus.COMPLETED
    assert recorded.confirmed == (SCOPE,)


def test_only_individual_demographics_is_a_standing_restriction() -> None:
    assert frozenset({INDIVIDUAL}) == STANDING_RESTRICTIONS


@pytest.fixture
def db() -> Iterator[Connection]:
    connection = customer_database()
    yield connection
    connection.close()


@pytest.mark.asyncio
async def test_refusals_recorded_by_the_query_service_are_read_back(
    db: Connection,
) -> None:
    setup = Setup(db, SNAPSHOT)
    assert isinstance(await setup.run("p.brand = 'Beta'", op="op-1"), QueryFailed)
    profile = await setup.service.execute(
        QueryAttempt(
            PRINCIPAL,
            "run-1",
            "op-2",
            1,
            AnalysisQuery(
                "SELECT c.age_band, COUNT(*) AS n FROM customers c "
                "WHERE c.customer_ref = @r GROUP BY c.age_band",
                {"r": "cus_1"},
            ),
        )
    )
    assert isinstance(profile, QueryFailed) and profile.rejected
    assert await setup.run("p.brand = 'Alpha'", op="op-3") is not None
    records = list(setup.operations.records.values())
    assert recorded_restrictions(records) == {SCOPE, INDIVIDUAL}
    # Other failures and successes are not restrictions.
    assert recorded_restrictions([r for r in records if r.operation_id == "op-3"]) == (
        NONE
    )


def test_the_model_output_carries_declined_restrictions() -> None:
    schema = AnswerOutput.model_json_schema()
    assert "declined" in schema["properties"]
    output = AnswerOutput.model_validate(
        {
            "text": "Demographics are group-level only; by age band: ...",
            "declined": ["individual_demographics", "individual_demographics"],
        }
    )
    result = cast(Any, SimpleNamespace(output=output, response=SimpleNamespace()))
    result.response.metadata = None
    draft = proposal("run-1", 0, result)
    assert draft.complete is True  # type: ignore[union-attr]
    assert draft.declined == (INDIVIDUAL,)  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="declined"):
        AnswerOutput.model_validate({"text": "x", "declined": ["budget"]})


def test_the_policy_says_a_declined_part_is_resolved() -> None:
    policy = render_investigation_policy(frozenset(), None)
    assert "is resolved, not unanswered" in policy
    assert "`declined`" in policy
