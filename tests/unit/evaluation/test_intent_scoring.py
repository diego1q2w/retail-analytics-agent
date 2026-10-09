"""Intended-question checks of the efficiency harness (T26-F9): generated
answers are scored for unlabelled causal claims, unrequested discovery and
period drift, not only prompt wording."""

from __future__ import annotations

import json
from pathlib import Path

from retail_analytics.application.contracts.evaluation import (
    EfficiencyRun,
    EfficiencySuite,
    EfficiencyTurn,
    QueryRecord,
    RepetitionResult,
    SpendCeiling,
)
from retail_analytics.application.evaluation.efficiency import (
    RunFacts,
    released_texts,
    rescore,
    resolve_figures,
    score_turn,
    unqualified_terms,
    unscoped_queries,
    worst_case,
)

ROOT = Path(__file__).resolve().parents[3]
SUITE = ROOT / "evaluation" / "real-model" / "efficiency" / "intent-suite.json"
EXPECTED = ROOT / "evaluation" / "realdata" / "expected.json"
CAUSES = ("traffic", "new customer", "seasonal", "weather", "marketing")

# Shaped on the released answers of the session that motivated T26-F9.
UNSUPPORTED = """\
## Drivers of growth
September revenue rose because of higher traffic and Seasonal Fall Wardrobe \
Demand.
### Recommendations
- Target the new customers acquired in September with a loyalty offer.
"""
LABELLED = """\
## Measured differences
Revenue rose 1,547.81 (6.2%) from September to October [evd_x].
Tops & Tees contributed most of the increase.
### Hypotheses (not tested by this data)
- Seasonal fall demand.
- Marketing activity.
### Recommended actions
- If seasonal demand is the driver, stock Tops & Tees earlier (hypothesis).
The data counts purchasing customers; it cannot show traffic or which \
customers are new.
"""


def _turn(**extra: object) -> EfficiencyTurn:
    data: dict[str, object] = {"text": "Compare", "kind": "investigation"}
    data.update(extra)
    return EfficiencyTurn.model_validate(data)


def _facts(text: str, tools: tuple[str, ...], *queries: QueryRecord) -> RunFacts:
    return RunFacts(
        run_id="run_1",
        run_status="completed",
        tools=tools,
        queries=queries,
        queries_before_question=None,
        asked_clarification=False,
        budget_tokens=0,
        active_seconds=1.0,
        wall_seconds=1.0,
        released_text=text,
        tables=(),
        session_evidence_ids=frozenset(),
    )


def _query(sql: str, **parameters: str) -> QueryRecord:
    return QueryRecord(outcome="succeeded", sql=sql, parameters=parameters)


def test_unlabelled_causes_in_headings_and_recommendations_are_misses() -> None:
    assert unqualified_terms(CAUSES, UNSUPPORTED) == (
        "traffic",
        "seasonal",
        "new customer",
    )


def test_labelled_hypotheses_and_limitations_pass() -> None:
    assert unqualified_terms(CAUSES, LABELLED) == ()
    # A disclaimer at the end does not label an earlier assertion.
    late = "Seasonal demand drove growth.\nThese are hypotheses."
    assert unqualified_terms(CAUSES, late) == ("seasonal",)
    # A qualified heading covers its section only, not the next one.
    sections = "### Hypotheses\n- Weather.\n### Findings\n- Weather lifted sales."
    assert unqualified_terms(CAUSES, sections) == ("weather",)


def test_queries_that_drop_the_conversation_period_are_counted() -> None:
    period = (("2025-10", "quarter"),)
    q4 = "SELECT 1 FROM s WHERE d >= @a AND d < @b a=2025-10-01 b=2026-01-01"
    all_time = "SELECT o.age_band, SUM(x) FROM s GROUP BY 1"
    assert unscoped_queries(period, [q4]) == 0
    assert unscoped_queries(period, [q4, all_time]) == 1
    assert unscoped_queries((), [all_time]) == 0


def test_tool_targets_and_claims_are_scored_and_rescored() -> None:
    spec = _turn(
        forbidden_tools=["list_relations", "execute_analysis"],
        required_tools=[["describe_relation", "list_relations"]],
        every_sql_terms=[["2025-10"]],
        qualified_terms=list(CAUSES),
    )
    bad = score_turn(
        1,
        spec,
        (),
        _facts(
            UNSUPPORTED,
            ("list_relations", "execute_analysis"),
            _query("SELECT SUM(x) FROM s"),
        ),
        [],
    )
    assert bad.targets_met["no_forbidden_tools"] is False
    assert bad.targets_met["required_tools"] is True
    assert bad.targets_met["every_query_scoped"] is False
    assert bad.targets_met["claims_qualified"] is False
    assert bad.unscoped_queries == 1
    assert "traffic" in bad.unqualified_terms
    good = score_turn(1, spec, (), _facts(LABELLED, ("describe_relation",)), [])
    assert all(good.targets_met.values()), good.targets_met
    # A schema-only overview that skipped discovery misses required_tools.
    none = score_turn(1, spec, (), _facts(LABELLED, ()), [])
    assert none.targets_met["required_tools"] is False
    # Saved runs keep the counts, so rescoring reproduces the targets.
    run = EfficiencyRun(
        label="x",
        suite_id="s",
        suite_version="1",
        scoring_version=0,
        recorded_at="-",
        code_revision="-",
        target_id="-",
        execution_backend="local",
        warehouse="-",
        data_ref="-",
        extract_digest="-",
        primary_provider="-",
        spend=SpendCeiling(
            max_total_tokens=1,
            max_model_attempts=1,
            run_tokens_limit=1,
            run_requests_limit=1,
        ),
        repetitions=(
            RepetitionResult(
                scenario_id="s1",
                repetition=1,
                session_id="-",
                executive_id="-",
                started_at="-",
                code_revision="-",
                turns=(bad.model_copy(update={"targets_met": {}}),),
            ),
        ),
    )
    suite = EfficiencySuite.model_validate(
        {
            "suite_id": "s",
            "suite_version": "1",
            "declared_on": "2026-10-09",
            "spend": run.spend.model_dump(),
            "scenarios": [
                {
                    "id": "s1",
                    "title": "t",
                    "category": "c",
                    "scope": "women",
                    "repeats": 1,
                    "turns": [spec.model_dump()],
                }
            ],
        }
    )
    (again,) = rescore(run, suite).repetitions[0].turns
    assert again.targets_met == bad.targets_met


def test_the_intent_suite_resolves_and_covers_the_acceptance_cases() -> None:
    suite = EfficiencySuite.model_validate_json(SUITE.read_text("utf-8"))
    expected = json.loads(EXPECTED.read_text("utf-8"))["queries"]
    values = {q: v["values"] for q, v in expected.items()}
    for scenario in suite.scenarios:
        for t in scenario.turns:
            assert len(resolve_figures(t, values)) == len(t.figures)
    worst = worst_case(suite)
    assert worst["total_tokens"] <= suite.spend.max_total_tokens
    assert worst["model_attempts"] <= suite.spend.max_model_attempts
    ids = {s.id: s for s in suite.scenarios}
    overview = ids["overview-approved-schema"].turns[0]
    assert {"list_relations", "describe_relation"} <= set(overview.forbidden_tools)
    assert ids["overview-missing-schema"].turns[0].required_tools
    spender = ids["spender-after-age-breakdown"].turns
    assert spender[1].text == "What age band is our biggest spender?"
    assert all(t.every_sql_terms for t in spender)
    assert {
        "individual-demographics-explicit",
        "aggregate-demographics-explicit",
    } <= set(ids)
    lifecycle = ids["comparison-report-lifecycle"].turns
    assert lifecycle[0].expect_report
    assert all(t.qualified_terms for t in lifecycle)
    assert ids["monthly-comparison"].turns[0].qualified_terms


def test_negated_limitations_from_a_live_report_pass() -> None:
    # Sentences released by the T26-F9 live run (saved report and answer).
    live = (
        "- Customer counts measure distinct purchasing buyers, not traffic or "
        "new acquisition cohorts.\n"
        "- **Limitations**: Customer metrics measure purchasing buyers, not "
        "website visitors or new customer acquisition cohorts. Changes by "
        "category represent measured sales contributions and do not establish "
        "external factors (such as weather or market trends) as causes.\n"
    )
    terms = (*CAUSES, "visitor", "acquisition")
    assert unqualified_terms(terms, live) == ()


def test_age_band_labels_match_across_dash_styles() -> None:
    spec = _turn(figures=[{"name": "band", "ref": "a.band", "kind": "label"}])
    figures = resolve_figures(spec, {"a": {"band": "65-69"}})
    result = score_turn(
        1, spec, figures, _facts(f"The 65{chr(0x2013)}69 band spent most.", ()), []
    )
    assert result.figures[0].in_answer


# The read/export turns of the T26-F9 report (scored v3 as a "promotion" miss).
REPORT_ACTIONS = """\
### Limitations
- Underlying causal drivers (such as traffic, seasonal demand shifts, marketing \
campaigns, or stockouts) are hypotheses not tested by transaction data alone.

### Recommended Actions
*(Recommendations are proposals to weigh; they are not observed results.)*
- **Inventory & Merchandising Support:** Review inventory, merchandising, and \
promotional support for high-growth categories such as Dresses and Sweaters.
"""
LIFECYCLE_TERMS = (*CAUSES, "campaign", "promotion", "acquired")


def test_a_lever_in_a_recommended_action_is_not_a_causal_claim() -> None:
    assert unqualified_terms(LIFECYCLE_TERMS, REPORT_ACTIONS) == ()
    labelled = (
        "- **Recommendation:** Review promotional support for Dresses. "
        "(based on [evd_x])"
    )
    assert unqualified_terms(LIFECYCLE_TERMS, labelled) == ()


def test_recommendations_that_attribute_a_cause_or_state_a_condition_fail() -> None:
    causal = "## Recommended actions\n- Extend the campaign, because it drove growth."
    assert unqualified_terms(LIFECYCLE_TERMS, causal) == ("campaign",)
    # Conditions the data cannot measure stay claims inside recommendations.
    assert "new customer" in unqualified_terms(LIFECYCLE_TERMS, UNSUPPORTED)
    seasonal = "### Next steps\n- Stock up for seasonal demand in Dresses."
    assert unqualified_terms(LIFECYCLE_TERMS, seasonal) == ("seasonal",)
    # Outside recommendations a lever is still a claim.
    finding = "## Findings\nThe promotion lifted Dresses."
    assert unqualified_terms(LIFECYCLE_TERMS, finding) == ("promotion",)


def test_released_texts_are_read_back_from_a_transcript() -> None:
    transcript = (
        "# Transcripts: x\n\n## s1 (repetition 1)\n\n"
        "### Turn 1: `run_a` (completed)\n\n**User:** Show it\n\n"
        "**Released:**\n\n## Summary\nRevenue rose.\n\n"
        "### Turn 2: `run_b` (partial)\n\n**Released:**\n\n_nothing_\n\n"
        "## s2 (repetition 1)\n\n### Turn 1: `run_c` (completed)\n\n"
        "**Released:**\n\nLast.\n"
    )
    assert released_texts(transcript) == {
        "run_a": "## Summary\nRevenue rose.",
        "run_b": "_nothing_",
        "run_c": "Last.",
    }


def test_the_recorded_t26f9_result_is_kept_and_the_rescoring_reproduces() -> None:
    results = SUITE.parent / "results"
    suite = EfficiencySuite.model_validate_json(SUITE.read_text("utf-8"))
    recorded = EfficiencyRun.model_validate_json(
        (results / "t26f9-intent.json").read_text("utf-8")
    )
    saved = EfficiencyRun.model_validate_json(
        (results / "t26f9-intent-rescored.json").read_text("utf-8")
    )
    assert recorded.scoring_version == 3 and recorded.rescored_from is None
    assert saved.rescored_from == "t26f9-intent"
    assert saved.rescored_from_scoring_version == 3

    def turns(run: EfficiencyRun) -> dict[tuple[str, int], dict[str, bool]]:
        return {
            (rep.scenario_id, t.turn): dict(t.targets_met)
            for rep in run.repetitions
            for t in rep.turns
        }

    before, after = turns(recorded), turns(saved)
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {
        ("comparison-report-lifecycle", 2),
        ("comparison-report-lifecycle", 3),
    }
    for key in changed:
        assert before[key]["claims_qualified"] is False
        assert after[key]["claims_qualified"] is True
    # The individual-demographics run stays a miss: it ended partial.
    assert after[("individual-demographics-explicit", 1)]["completed"] is False
    texts: dict[str, str] = {}
    for transcript in {r.transcript for r in recorded.repetitions if r.transcript}:
        texts.update(released_texts((results / transcript).read_text("utf-8")))
    again = rescore(recorded, suite, texts)
    assert turns(again) == after
