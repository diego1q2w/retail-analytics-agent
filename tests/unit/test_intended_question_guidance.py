"""The intended question (T26-F9): what the model is told about schema
overviews, individual versus group wording, carried periods and labelled
hypotheses. Prompt checks only; the generated answers are scored by the
intent suite (``evaluation/real-model/efficiency/intent-suite.json``)."""

from __future__ import annotations

from retail_analytics.application import investigation_policy as policy
from retail_analytics.application import tool_focus
from retail_analytics.application.skill_assets import investigation, saved_reports

ALL_TOOLS = frozenset(
    value
    for name, value in vars(policy).items()
    if name.isupper() and isinstance(value, str)
)
SECTION = "The intended question and its explanations:"


def _section(text: str, title: str) -> str:
    start = text.index(title)
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else None]


def test_overviews_use_the_approved_schema_and_discovery_only_for_gaps() -> None:
    text = policy.render_investigation_policy(ALL_TOOLS)
    assert "answered from <approved_schema> when it is present" in text
    assert "without calling list_relations, describe_relation" in text
    assert "Use list_relations, describe_relation only when <approved_schema> " in (
        text
    )
    assert "is missing or unavailable" in text
    # Discovery is narrowed, not disabled.
    no_discovery = policy.render_investigation_policy(
        ALL_TOOLS - {policy.LIST_RELATIONS, policy.DESCRIBE_RELATION}
    )
    assert "list_relations" not in no_discovery
    assert "otherwise from what you know you can do" in no_discovery


def test_wording_keeps_individual_or_group_meaning_and_the_period() -> None:
    rules = _section(policy.render_investigation_policy(ALL_TOOLS), SECTION)
    assert "what age band is our biggest spender?" in rules
    assert "answer the group reading and say so in your first sentence" in rules
    assert "Never silently answer a different question" in rules
    # The individual restriction itself stays in Safety (one source).
    assert "declined as Safety says" in rules
    safety = _section(policy.render_investigation_policy(ALL_TOOLS), "Safety:")
    assert "Never give a demographic for one customer" in safety
    assert "keeps the conversation's period, metric, definition" in rules
    assert "Do not run an all-time or other-period query" in rules
    # Without queries there is nothing to re-run.
    no_query = policy.render_investigation_policy({policy.FETCH_EVIDENCE})
    assert "all-time" not in no_query


def test_explanations_stay_measured_and_hypotheses_are_labelled() -> None:
    rules = _section(policy.render_investigation_policy(ALL_TOOLS), SECTION)
    assert "not site visitors or traffic, and not necessarily newly acquired" in (rules)
    assert "does not establish seasonality, weather, marketing" in rules
    assert "in headings, summaries and recommended actions too" in rules
    assert "not only in a closing disclaimer" in rules
    assert "investigate further instead" in rules  # no blanket refusal
    assert "saved, read or exported keep the answer's uncertainty" in rules
    # One home for the causal rule.
    full = policy.render_investigation_policy(ALL_TOOLS)
    assert full.count("do not claim causes the data cannot show") == 1
    assert policy.render_investigation_policy(frozenset()).count(SECTION) == 0


def test_new_skill_versions_carry_the_uncertainty_rules() -> None:
    assert tool_focus.CURRENT["investigation"] is investigation.V2
    assert tool_focus.CURRENT["saved_reports"] is saved_reports.V2
    analysis = investigation.V2.render(frozenset({policy.EXECUTE_ANALYSIS}))
    assert "labelled hypotheses, including in headings" in analysis
    assert "Buyer counts are not traffic or acquisition" in analysis
    reports = saved_reports.V2.render(frozenset({policy.SAVE_REPORT}))
    assert "hypotheses stay labelled in its title, headings" in reports
    assert "without strengthening its claims" in reports
    # Shipped versions are never edited: V1 still renders its own text.
    assert "labelled hypotheses" not in investigation.V1.render(
        frozenset({policy.EXECUTE_ANALYSIS})
    )
    assert tool_focus.SKILL_VERSIONS["saved_reports"][1] is saved_reports.V1
