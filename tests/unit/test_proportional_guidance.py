"""Proportional work: what the model is told about answer shape and budget.

These are prompt and accounting checks only. Whether the real model follows
the guidance is measured by the live walkthrough
(``evaluation/real-model/proportion_walkthrough.py``), not here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from retail_analytics.adapters.agent.investigator import AnswerOutput
from retail_analytics.adapters.models.budgeted import BudgetedModel
from retail_analytics.application import investigation_policy as policy
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.investigation_runtime import (
    RunStopped,
    _budget_line,
)
from retail_analytics.domain.budgets import BudgetResource, RunLimits
from tests.unit.budgets.memory_store import MemoryRunBudgetStore

NOW = datetime(2026, 10, 9, tzinfo=UTC)
ALL_TOOLS = frozenset(
    value
    for name, value in vars(policy).items()
    if name.isupper() and isinstance(value, str)
)
ANALYSIS_ONLY = frozenset(
    {policy.LIST_RELATIONS, policy.DESCRIBE_RELATION, policy.EXECUTE_ANALYSIS}
)


def _section(text: str, title: str) -> str:
    start = text.index(title)
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else None]


@pytest.mark.parametrize("tools", [ALL_TOOLS, ANALYSIS_ONLY])
def test_a_figure_question_stops_once_evidence_supports_it(
    tools: frozenset[str],
) -> None:
    text = policy.render_investigation_policy(tools)
    assert "A figure question is answered once cited evidence" in text
    assert "supports its metric, period and answer: stop there" in text
    # Further work needs a reason, and missing or invalid evidence is one.
    assert "Query again only for a part of the request that is still" in text
    assert "evidence that is missing, truncated or no longer valid" in text
    assert "concrete inconsistency between results" in text
    assert "add no breakdowns (daily, status, product, earlier periods)" in text
    # The adaptive loop stays: investigations are not capped.
    assert "Genuine investigations (why, what drives, reports)" in text
    assert "as many queries as their open questions need" in text
    assert not re.search(r"at most \d+ quer", text)


def test_ordinary_wording_resolves_and_only_material_ambiguity_is_asked() -> None:
    text = policy.render_investigation_policy(ALL_TOOLS)
    assert "a month without a year is its most recent occurrence" in text
    assert "state that interpretation in the answer" in text
    assert "only when a material ambiguity remains" in text
    assert "never compute several speculative interpretations" in text


def test_answer_shapes_cover_each_kind_of_request_without_figures() -> None:
    text = policy.render_investigation_policy(ALL_TOOLS)
    shapes = _section(text, "Answer shapes (")
    for request in (
        "What was revenue in September?",
        "And August?",
        "How did September compare with August?",
        "Why did revenue fall in September?",
        "Prepare a report on third-quarter sales with recommendations",
    ):
        assert f'"{request}"' in shapes
    # Worked examples never carry figures a model could repeat.
    assert not re.search(r"\d", shapes)
    assert "No daily or status breakdowns, history or actions" in shapes
    assert "recommended actions kept apart from the findings" in shapes
    assert "measured contributors" in shapes


def test_reports_keep_findings_and_actions() -> None:
    text = policy.render_investigation_policy(ALL_TOOLS)
    assert (
        "findings, limitations and suggested actions are for investigations "
        "and reports" in text
    )
    assert f"{policy.SAVE_REPORT} when the user asks for a report" in text
    assert "recommended actions are separate from findings" in text


def test_golden_methods_are_for_unclear_methods_not_default_metrics() -> None:
    text = policy.render_investigation_policy(ALL_TOOLS)
    assert f"genuinely unclear, {policy.FIND_EXAMPLES}" in text
    assert "a default metric such as revenue does not need them" in text


def test_preferences_in_context_need_no_lookup() -> None:
    text = policy.render_investigation_policy(ALL_TOOLS)
    assert "effective preferences are already in <preferences>" in text
    assert f"{policy.INSPECT_PREFERENCES} only when the user asks" in text
    without = policy.render_investigation_policy(ANALYSIS_ONLY)
    assert policy.INSPECT_PREFERENCES not in without


def test_no_query_capability_means_no_query_guidance() -> None:
    schema_only = frozenset({policy.LIST_RELATIONS, policy.DESCRIBE_RELATION})
    text = policy.render_investigation_policy(schema_only)
    assert "Answer shapes" not in text
    assert "a figure question gets the figure, not a report" in text
    assert "A figure question is answered once" not in text


def test_answer_schema_agrees_with_the_prompt_about_shape() -> None:
    description = AnswerOutput.model_fields["text"].description or ""
    text = policy.render_investigation_policy(ALL_TOOLS)
    assert "for a figure question the figure" in description
    assert "only for investigations and reports" in description
    assert "a figure question gets the figure, not a report" in text
    # The old schema asked every answer for findings and actions.
    assert description != "The answer: findings, limitations and suggested actions."


# Budget visibility


async def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart("Done.")],
        usage=RequestUsage(input_tokens=1_000, output_tokens=200),
    )


def _model(budgets: RunBudgets, key: str) -> BudgetedModel:
    return BudgetedModel(
        FunctionModel(_respond),
        budgets,
        run_id=lambda: "run",
        request_key=lambda _: key,
    )


MESSAGES: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Revenue?")])]


@pytest.mark.asyncio
async def test_budget_line_shows_accounted_tokens_as_they_decrease() -> None:
    budgets = RunBudgets(MemoryRunBudgetStore(), RunLimits(), clock=lambda: NOW)
    await budgets.open("run")
    first = await budgets.snapshot("run")
    line = _budget_line(first)
    assert "model requests left: 20 of 20" in line
    assert "tokens left: 100000 of 100000\n" in line
    assert "queries left: 10 of 10" in line
    assert "limits, not targets: answer as soon as the evidence" in line

    for attempt in range(2):
        await _model(budgets, f"a{attempt}").request(
            MESSAGES, None, ModelRequestParameters()
        )
    after = await budgets.snapshot("run")
    assert after is not None
    line = _budget_line(after)
    # Exactly what accounting recorded: 2 x (1,000 in + 200 out).
    assert after.usage.tokens == 2_400
    assert "model requests left: 18 of 20" in line
    assert "tokens left: 97600 of 100000 (about 1200 per model request" in line
    assert int(after.remaining()[BudgetResource.TOKENS]) == 97_600
    # Rendering reads; it charges nothing.
    assert await budgets.snapshot("run") == after


@pytest.mark.asyncio
async def test_low_budget_asks_for_a_conclusion_and_enforcement_is_unchanged() -> None:
    budgets = RunBudgets(
        MemoryRunBudgetStore(),
        RunLimits(tokens=2_400, provider_requests=20),
        clock=lambda: NOW,
    )
    await _model(budgets, "a0").request(MESSAGES, None, ModelRequestParameters())
    snapshot = await budgets.snapshot("run")
    line = _budget_line(snapshot)
    assert "tokens left: 1200 of 2400" in line
    assert "nearly spent: answer now from the evidence you have" in line
    # Answered requests finish normally; incomplete only if work remains.
    assert "finish normally" in line
    assert "do not call the answer incomplete" in line
    assert "Only if requested work is still unanswered" in line
    # The line is advice; the budget still refuses work past its limit.
    await _model(budgets, "a1").request(MESSAGES, None, ModelRequestParameters())
    with pytest.raises(RunStopped) as stopped:
        await _model(budgets, "a2").request(MESSAGES, None, ModelRequestParameters())
    assert stopped.value.resource is BudgetResource.TOKENS


@pytest.mark.asyncio
async def test_few_requests_left_asks_for_a_conclusion() -> None:
    budgets = RunBudgets(
        MemoryRunBudgetStore(), RunLimits(provider_requests=3), clock=lambda: NOW
    )
    await _model(budgets, "a0").request(MESSAGES, None, ModelRequestParameters())
    line = _budget_line(await budgets.snapshot("run"))
    assert "model requests left: 2 of 3" in line
    assert "nearly spent" in line


def test_unknown_budget_stays_unknown() -> None:
    assert _budget_line(None) == "<budget>unknown</budget>"


def test_policy_states_the_join_rule_and_points_at_the_documented_example() -> None:
    from retail_analytics.application.contracts.sql_dialect import SQL_JOIN_RULE

    text = policy.render_investigation_policy(ALL_TOOLS)
    assert SQL_JOIN_RULE in text
    assert "scalar subquery for the latest year" in text
    assert "can find the latest September" not in text
    assert SQL_JOIN_RULE not in policy.render_investigation_policy(
        ALL_TOOLS - {policy.EXECUTE_ANALYSIS}
    )


@pytest.mark.asyncio
async def test_little_active_time_left_asks_for_a_conclusion() -> None:
    from datetime import timedelta

    now = [NOW]
    budgets = RunBudgets(MemoryRunBudgetStore(), RunLimits(), clock=lambda: now[0])
    await budgets.open("run")
    line = _budget_line(await budgets.snapshot("run"))
    assert "active seconds left: 120 of 120 (the investigation stops at 0)" in line
    assert "nearly spent" not in line
    now[0] = NOW + timedelta(seconds=95)
    line = _budget_line(await budgets.snapshot("run"))
    assert "active seconds left: 25 of 120" in line
    assert "nearly spent: answer now from the evidence you have" in line
