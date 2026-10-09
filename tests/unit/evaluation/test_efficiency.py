"""Conversation efficiency suite: references, scoring, spend and summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retail_analytics.application.contracts.evaluation import (
    EfficiencyRun,
    EfficiencyScenario,
    EfficiencySuite,
    EfficiencyTurn,
    ObservedTable,
    QueryRecord,
    RecordedSpan,
    RepetitionResult,
    SpendCeiling,
)
from retail_analytics.application.contracts.telemetry import Span
from retail_analytics.application.evaluation.efficiency import (
    SCORING_VERSION,
    RunFacts,
    SuiteError,
    aggregate,
    attempted_query,
    query_outcome,
    render_summary,
    rescore,
    resolve_figures,
    score_turn,
    spend_allows,
    spend_used,
    worst_case,
)

ROOT = Path(__file__).resolve().parents[3]
SUITE = ROOT / "evaluation" / "real-model" / "efficiency" / "suite.json"
EXPECTED = ROOT / "evaluation" / "realdata" / "expected.json"
VALUES = {"monthly_trend": {"month_1_revenue": 100.0, "month_2_revenue": 130.5}}
CEILING = SpendCeiling(
    max_total_tokens=1000, max_model_attempts=50, run_tokens_limit=300,
    run_requests_limit=10,
)  # fmt: skip


def turn(**extra: object) -> EfficiencyTurn:
    data: dict[str, object] = {
        "text": "What was revenue in August?",
        "kind": "cold",
        "targets": {"max_queries": 1, "max_model_requests": 2},
        "figures": [{"name": "aug", "ref": "monthly_trend.month_2_revenue"}],
        "text_terms": [["august"]],
        "sql_terms": [["complete"], ["2025-08"]],
    }
    data.update(extra)
    return EfficiencyTurn.model_validate(data)


def table(*rows: tuple[object, ...]) -> ObservedTable:
    return ObservedTable(
        evidence_id="evd_" + "a" * 32,
        columns=("v",),
        roles=("value",),
        sources=((),),
        rows=tuple(rows),  # type: ignore[arg-type]
        truncated=False,
        scope_matches=True,
    )


def attempt(run: str, outcome: str, tokens: int = 10) -> RecordedSpan:
    return RecordedSpan(
        Span.MODEL_ATTEMPT.value,
        run,
        {
            "provider": "p",
            "model": "m",
            "outcome": outcome,
            "input_tokens": tokens if outcome == "succeeded" else 0,
            "output_tokens": 1 if outcome == "succeeded" else 0,
            "fallback_from": "none",
        },
    )


def facts(text: str, *queries: QueryRecord, rows: int = 1) -> RunFacts:
    return RunFacts(
        run_id="run_1",
        run_status="completed",
        tools=("execute_analysis",),
        queries=queries,
        queries_before_question=None,
        asked_clarification=False,
        budget_tokens=0,
        active_seconds=3.0,
        wall_seconds=3.5,
        released_text=text,
        tables=(table(*[(130.5,)] * rows),),
        session_evidence_ids=frozenset({"evd_" + "a" * 32}),
    )


OK_QUERY = QueryRecord(
    outcome="succeeded",
    sql="SELECT SUM(x) FROM s WHERE status = @a AND d >= @b",
    parameters={"a": "Complete", "b": "2025-08-01"},
)


def test_the_committed_suite_resolves_against_the_independent_references() -> None:
    suite = EfficiencySuite.model_validate_json(SUITE.read_text("utf-8"))
    expected = json.loads(EXPECTED.read_text("utf-8"))["queries"]
    values = {q: v["values"] for q, v in expected.items()}
    for scenario in suite.scenarios:
        for t in scenario.turns:
            assert len(resolve_figures(t, values)) == len(t.figures)
    # The spend ceiling covers the suite's worst case.
    worst = worst_case(suite)
    assert worst["total_tokens"] <= suite.spend.max_total_tokens
    assert worst["model_attempts"] <= suite.spend.max_model_attempts


def test_derived_differences_and_unknown_references() -> None:
    derived = turn(
        figures=[
            {
                "name": "d",
                "ref": "monthly_trend.month_2_revenue",
                "minus": "monthly_trend.month_1_revenue",
            }
        ]
    )
    (figure,) = resolve_figures(derived, VALUES)
    assert figure.value == 30.5
    with pytest.raises(SuiteError):
        resolve_figures(turn(figures=[{"name": "x", "ref": "nope.value"}]), VALUES)


def test_a_turn_within_targets_with_right_figures() -> None:
    spans = [attempt("run_1", "succeeded", 100), attempt("run_1", "succeeded", 50)]
    result = score_turn(
        1,
        turn(),
        resolve_figures(turn(), VALUES),
        facts("August revenue was 130.50 [evd_" + "a" * 32 + "].", OK_QUERY),
        spans,
    )
    assert result.targets_met == {
        "completed": True,
        "no_unexpected_question": True,
        "queries": True,
        "model_requests": True,
    }
    assert result.figures[0].in_answer and result.figures[0].in_evidence
    assert result.text_terms_met == (True,)
    assert result.sql_terms_met == (True, True)
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (
        150,
        2,
        152,
    )
    assert (result.citations, result.cited_unknown) == (1, 0)
    assert result.extra_queries == 0


def test_failed_attempts_and_extra_queries_count_against_targets() -> None:
    spans = [
        attempt("run_1", "failed"),
        attempt("run_1", "succeeded"),
        attempt("run_1", "succeeded"),
        attempt("other", "succeeded"),
        RecordedSpan(
            Span.CONTEXT_RESTART.value, "run_1", {"restart.cause": "history_changed"}
        ),
    ]
    failed = QueryRecord(outcome="failed", error_code="invalid_sql")
    result = score_turn(
        1,
        turn(),
        resolve_figures(turn(), VALUES),
        facts("It was 120 [evd_" + "b" * 32 + "].", OK_QUERY, OK_QUERY, failed),
        spans,
    )
    assert result.model_requests == 3 and result.model_requests_failed == 1
    assert result.targets_met == {
        "completed": True,
        "no_unexpected_question": True,
        "queries": False,
        "model_requests": False,
    }
    assert (result.queries_succeeded, result.queries_failed) == (2, 1)
    assert result.extra_queries == 1
    assert result.restarts == ("history_changed",)
    assert not result.figures[0].in_answer
    assert result.cited_unknown == 1


def test_clarification_and_report_targets() -> None:
    spec = turn(
        kind="clarification",
        expect_clarification=True,
        expect_report=True,
        targets={"max_queries_before_question": 0},
    )
    base = facts("Saved.")
    asked = RunFacts(
        **{
            **{k: getattr(base, k) for k in base.__slots__},
            "asked_clarification": True,
            "queries_before_question": 0,
            "report_saved": True,
            "report_actions": 0,
        }
    )
    result = score_turn(1, spec, (), asked, [])
    assert result.targets_met == {
        "completed": True,
        "queries_before_question": True,
        "asked_clarification": True,
        "report_with_actions": False,
    }


def test_spend_ceiling_refuses_work_that_could_cross_it() -> None:
    assert spend_allows({}, CEILING, 3)
    assert not spend_allows({"total_tokens": 200}, CEILING, 3)
    assert not spend_allows({"model_attempts": 45}, CEILING, 1)


def _run(label: str, tokens: int) -> EfficiencyRun:
    spans = [attempt("run_1", "succeeded", tokens)]
    scored = score_turn(
        1, turn(), resolve_figures(turn(), VALUES), facts("130.5", OK_QUERY), spans
    )
    reps = (
        *(
            RepetitionResult(
                scenario_id="scalar",
                repetition=i,
                session_id="ses_1",
                executive_id="eval-1",
                started_at="2026-10-09T00:00:00+00:00",
                code_revision="abc1234",
                turns=(scored,),
            )
            for i in (1, 2)
        ),
        RepetitionResult(
            scenario_id="scalar",
            repetition=3,
            session_id="-",
            executive_id="-",
            started_at="2026-10-09T00:00:00+00:00",
            code_revision="abc1234",
            error="TimeoutError",
        ),
    )
    return EfficiencyRun(
        label=label,
        suite_id="s",
        suite_version="1",
        recorded_at="2026-10-09T00:00:00+00:00",
        code_revision="abc1234",
        target_id="agent_runtime:local",
        execution_backend="local",
        warehouse="duckdb",
        data_ref="d",
        extract_digest="0" * 64,
        primary_provider="p",
        spend=CEILING,
        spend_used=spend_used(reps),
        repetitions=reps,
    )


def test_summary_reports_every_repetition_and_the_baseline_change() -> None:
    baseline, candidate = _run("baseline", 200), _run("candidate", 100)
    stats = aggregate(candidate)[("scalar", 1)]
    assert stats.n == 2 and stats.targets_met == 2 and stats.figures_ok == 2
    assert candidate.spend_used == {
        "total_tokens": 202,
        "model_attempts": 2,
        "runs": 2,
    }
    text = render_summary(candidate, baseline)
    assert "ERROR TimeoutError" in text  # failed repetitions stay visible
    assert text.count("| scalar | ") >= 4
    assert "200 -> 100 (-50%)" in text


def test_unanswered_turns_never_meet_targets_and_saved_runs_rescore() -> None:
    base = facts("What would you like to analyze?")
    asked = RunFacts(
        **{
            **{k: getattr(base, k) for k in base.__slots__},
            "run_status": "cancelled",
            "queries": (),
            "asked_clarification": True,
        }
    )
    result = score_turn(1, turn(), resolve_figures(turn(), VALUES), asked, [])
    assert not result.targets_met["completed"]
    assert not result.targets_met["no_unexpected_question"]
    # A run scored by the first scoring version gains the new targets.
    old = _run("baseline", 100)
    stale = old.model_copy(
        update={
            "scoring_version": 1,
            "repetitions": tuple(
                r.model_copy(
                    update={
                        "turns": tuple(
                            t.model_copy(update={"targets_met": {"queries": True}})
                            for t in r.turns
                        )
                    }
                )
                for r in old.repetitions
            ),
        }
    )
    suite = EfficiencySuite(
        suite_id="s",
        suite_version="1",
        declared_on="2026-10-09",
        spend=CEILING,
        scenarios=(
            EfficiencyScenario(
                id="scalar", title="t", category="c", scope="women", repeats=3,
                turns=(turn(),),
            ),
        ),
    )  # fmt: skip
    fresh = rescore(stale, suite)
    assert fresh.scoring_version == SCORING_VERSION
    assert "completed" in fresh.repetitions[0].turns[0].targets_met


def test_rejected_attempts_are_counted_apart_from_warehouse_queries() -> None:
    rejected = QueryRecord(
        outcome=query_outcome(
            status="failed",
            compiler_rejected=True,
            executed=False,
            error_code="UNSUPPORTED_SQL",
        ),
        error_code="UNSUPPORTED_SQL",
        executed=False,
        reason="compile_unsupported_join",
        tool_result="failed",
        attempted_sql="WITH t AS (SELECT 1 AS complete) SELECT 2025-08 FROM s JOIN t",
    )
    warehouse_failure = QueryRecord(
        outcome=query_outcome(
            status="failed",
            compiler_rejected=False,
            executed=True,
            error_code="TEMPORARY_FAILURE",
        ),
        executed=True,
    )
    assert rejected.outcome == "rejected"
    assert warehouse_failure.outcome == "failed"
    # Refused by input validation before any operation existed.
    assert (
        query_outcome(
            status=None,
            compiler_rejected=False,
            executed=False,
            error_code="INVALID_INPUT",
        )
        == "rejected"
    )
    assert (
        query_outcome(
            status=None,
            compiler_rejected=False,
            executed=False,
            error_code="BUDGET_EXCEEDED",
        )
        == "failed"
    )
    result = score_turn(
        1,
        turn(sql_terms=[["join"]]),
        resolve_figures(turn(), VALUES),
        facts("x", rejected, OK_QUERY, warehouse_failure),
        [attempt("run_1", "succeeded")],
    )
    assert (
        result.queries_succeeded,
        result.queries_rejected,
        result.queries_failed,
    ) == (1, 1, 1)
    assert result.targets_met["queries"]  # one warehouse query: within target
    # A rejected attempt's SQL never meets SQL terms.
    assert result.sql_terms_met == (False,)
    run = _run("candidate", 10)
    first = run.repetitions[0].model_copy(update={"turns": (result,)})
    summary = render_summary(run.model_copy(update={"repetitions": (first,)}), None)
    assert "| 1/1/1 |" in summary


def test_attempted_query_reads_the_compiler_span_of_the_operation() -> None:
    def compile_span(op: str, sql: str) -> RecordedSpan:
        return RecordedSpan(
            Span.COMPILE.value,
            "run_1",
            {"operation_id": op, "outcome": "rejected"},
            {"inputs": {"generated_sql": sql, "parameters": {"month": 9}}},
        )

    spans = [compile_span("op1", "SELECT a"), compile_span("op2", "SELECT b")]
    assert attempted_query(spans, "op2") == ("SELECT b", {"month": "9"})
    assert attempted_query(spans, "op3") == (None, {})


def test_recorder_keeps_sanitized_compiler_content_only() -> None:
    from retail_analytics.adapters.evaluation.telemetry_recorder import (
        RecordingTelemetrySink,
    )
    from retail_analytics.application.telemetry import (
        Telemetry,
        telemetry,
        use_telemetry,
    )

    sink = RecordingTelemetrySink()
    with use_telemetry(Telemetry(sink)):
        for name in (Span.COMPILE, Span.TOOL):
            with telemetry().span(
                name, run_id="run_1", attributes={"operation_id": "op1"}
            ) as span:
                span.inputs(
                    {"generated_sql": "SELECT 'jane@example.com'", "parameters": {}}
                )
    compiled, tool = sink.spans()
    sql, _ = attempted_query([compiled], "op1")
    assert sql is not None and sql.startswith("SELECT")
    assert "jane@example.com" not in sql  # sanitized by the facade
    assert tool.content == {}
