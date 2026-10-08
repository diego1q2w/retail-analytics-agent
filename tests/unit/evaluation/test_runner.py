"""Runner self-tests: known good/bad cases, tolerances, zero denominators."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from click.testing import CliRunner

from retail_analytics.adapters.evaluation.files import (
    ReplayTarget,
    load_result,
    write_result,
)
from retail_analytics.application.evaluation.compare import compare_runs
from retail_analytics.application.evaluation.manifest import Manifest
from retail_analytics.application.evaluation.ports import (
    JudgeScoreOut,
    ScenarioInput,
    TargetObservation,
    TargetUnavailable,
)
from retail_analytics.application.evaluation.results import (
    RunResult,
    SensitiveContentError,
    ratio,
    serialize_result,
)
from retail_analytics.application.evaluation.runner import RunConfig, run_manifest
from retail_analytics.bootstrap.evaluate import main

SCOPE = {"executive_ref": "exec-1", "product_scope": ["prod-a"]}


def scenario(sid: str, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": sid,
        "title": "t",
        "level": 1,
        "category": "calc",
        "verification": ["deterministic"],
        "scope": SCOPE,
        "dialogue": [{"text": "What was revenue?"}],
        "expectations": [
            {"kind": "numeric", "name": "revenue", "expected": 100.0, "rel_tol": 0.01}
        ],
    }
    base.update(over)
    return base


def manifest(*scenarios: dict[str, object]) -> Manifest:
    return Manifest.model_validate(
        {"manifest_id": "m", "manifest_version": "1", "scenarios": list(scenarios)}
    )


class FakeTarget:
    target_id = "fake"

    def __init__(self, obs: Mapping[str, TargetObservation | Exception]) -> None:
        self.obs = obs
        self.seen: list[ScenarioInput] = []

    def run(self, case: ScenarioInput) -> TargetObservation:
        self.seen.append(case)
        item = self.obs[case.scenario_id]
        if isinstance(item, Exception):
            raise item
        return item


def obs(**values: float | str | bool) -> TargetObservation:
    return TargetObservation(answer_text="answer", values=values)


def test_known_good_and_bad_and_status_distinction() -> None:
    m = manifest(
        scenario("good"),
        scenario("bad"),
        scenario("planned", implementation_status="planned"),
        scenario("live", mode="live", requires=["bigquery"]),
        scenario("down"),
        scenario("boom"),
    )
    target = FakeTarget(
        {
            "good": obs(revenue=100.5),
            "bad": obs(revenue=130.0),
            "down": TargetUnavailable(),
            "boom": RuntimeError("secret@example.com failed"),
        }
    )
    result = run_manifest(m, target, RunConfig())
    status = {c.scenario_id: c.status for c in result.cases}
    assert status == {
        "good": "passed",
        "bad": "failed",
        "planned": "skipped",
        "live": "skipped",
        "down": "blocked",
        "boom": "errored",
    }
    assert result.verdict == "failed"
    boom = next(c for c in result.cases if c.scenario_id == "boom")
    assert boom.error_type == "RuntimeError"
    assert "example.com" not in serialize_result(result)


def test_missing_credentials_block_and_never_pass() -> None:
    m = manifest(scenario("live", mode="live", requires=["bigquery"]))
    target = FakeTarget({"live": obs(revenue=100.0)})
    blocked = run_manifest(m, target, RunConfig(mode="live"))
    assert blocked.cases[0].status == "blocked"
    assert blocked.cases[0].blocked_on == ("bigquery",)
    assert blocked.verdict == "incomplete"
    assert target.seen == []
    ok = run_manifest(
        m,
        target,
        RunConfig(mode="live", available_capabilities=frozenset({"bigquery"})),
    )
    assert ok.verdict == "passed"


def test_all_skipped_is_incomplete_not_passed() -> None:
    m = manifest(scenario("p", implementation_status="planned"))
    assert run_manifest(m, FakeTarget({}), RunConfig()).verdict == "incomplete"


@pytest.mark.parametrize(
    ("observed", "ok"),
    [
        (100.0, True),
        (101.0, True),
        (101.01, False),
        (99.0, True),
        (float("nan"), False),
    ],
)
def test_relative_tolerance(observed: float, ok: bool) -> None:
    m = manifest(scenario("s"))
    result = run_manifest(m, FakeTarget({"s": obs(revenue=observed)}), RunConfig())
    assert result.cases[0].checks[0].passed is ok


def test_absolute_tolerance_and_zero_expected() -> None:
    exp = [{"kind": "numeric", "name": "x", "expected": 0.0, "abs_tol": 0.005}]
    m = manifest(scenario("s", expectations=exp))
    good = run_manifest(m, FakeTarget({"s": obs(x=0.004)}), RunConfig())
    bad = run_manifest(m, FakeTarget({"s": obs(x=0.006)}), RunConfig())
    assert good.cases[0].status == "passed"
    assert bad.cases[0].status == "failed"


def test_missing_wrong_type_and_exact_type_strictness() -> None:
    exp = [
        {"kind": "numeric", "name": "n", "expected": 1.0},
        {"kind": "exact", "name": "flag", "expected": True},
        {"kind": "exact", "name": "label", "expected": "Alpha"},
    ]
    m = manifest(scenario("s", expectations=exp))
    result = run_manifest(
        m, FakeTarget({"s": obs(n="1", flag=1, label="Alpha")}), RunConfig()
    )
    details = {c.name: c.detail for c in result.cases[0].checks}
    assert details == {"n": "wrong_type", "flag": "mismatch", "label": "ok"}
    label = result.cases[0].checks[2]
    assert label.expected_digest == label.observed_digest
    assert "Alpha" not in serialize_result(result)


def test_text_and_tool_checks_keep_only_digests() -> None:
    exp = [
        {"kind": "not_contains", "name": "no_leak", "needle": "canary-9f2"},
        {"kind": "tool_not_called", "name": "no_sql", "tool": "run_sql"},
        {"kind": "tool_called", "name": "uses", "tool": "report"},
    ]
    m = manifest(scenario("s", expectations=exp))
    leaked = TargetObservation(answer_text="x canary-9f2", tool_calls=("run_sql",))
    result = run_manifest(m, FakeTarget({"s": leaked}), RunConfig())
    assert [c.passed for c in result.cases[0].checks] == [False, False, False]
    assert "canary-9f2" not in serialize_result(result)


def test_zero_denominator_ratios_are_null() -> None:
    assert ratio(0, 0).value is None
    m = manifest(scenario("p", implementation_status="planned"))
    agg = run_manifest(m, FakeTarget({}), RunConfig()).aggregates
    assert agg.numeric_correctness.value is None
    assert agg.task_completion.denominator == 0
    assert agg.safety_gate_failures.value is None


def test_identical_runs_have_identical_verdicts_and_counts() -> None:
    m = manifest(scenario("a"), scenario("b"))
    o = {"a": obs(revenue=100.0), "b": obs(revenue=1.0)}
    r1 = run_manifest(m, FakeTarget(o), RunConfig())
    r2 = run_manifest(m, FakeTarget(o), RunConfig())
    assert serialize_result(r1) == serialize_result(r2)
    assert r1.verdict_digest == r2.verdict_digest
    assert r1.aggregates.numeric_correctness.denominator == 2
    assert r1.cases[0].sample_count == 1
    assert r1.recorded_at is None


def test_judge_scores_are_separate_and_never_gate() -> None:
    spec = {"rubric_id": "rub-1", "dimensions": ["grounding"]}
    m = manifest(
        scenario("j", verification=["deterministic", "judge"], judge=spec),
    )

    class Judge:
        judge_id = "judge-a"

        def score(
            self,
            case: ScenarioInput,
            observation: TargetObservation,
            rubric_id: str,
            dimensions: object,
        ) -> list[JudgeScoreOut]:
            return [JudgeScoreOut(dimension="grounding", score=0.2)]

    target = FakeTarget({"j": obs(revenue=100.0)})
    without = run_manifest(m, target, RunConfig())
    assert without.cases[0].status == "blocked"
    assert without.cases[0].reason == "judge_unavailable"
    scored = run_manifest(m, target, RunConfig(), judges=[Judge()])
    assert scored.cases[0].status == "passed"
    assert scored.aggregates.judge[0].mean == 0.2
    assert all("grounding" not in str(c) for c in scored.cases[0].checks)


def test_failed_deterministic_check_wins_over_missing_judge() -> None:
    spec = {"rubric_id": "rub-1", "dimensions": ["grounding"]}
    m = manifest(scenario("j", verification=["deterministic", "judge"], judge=spec))
    result = run_manifest(m, FakeTarget({"j": obs(revenue=1.0)}), RunConfig())
    assert result.cases[0].status == "failed"


def test_target_never_sees_expectations() -> None:
    m = manifest(scenario("s"))
    target = FakeTarget({"s": obs(revenue=100.0)})
    run_manifest(m, target, RunConfig())
    assert "expect" not in target.seen[0].model_dump_json()


def test_manifest_validation_rejects_inconsistent_scenarios() -> None:
    with pytest.raises(ValueError, match="deterministic verification"):
        manifest(scenario("s", expectations=[]))
    with pytest.raises(ValueError, match="unique"):
        manifest(scenario("s"), scenario("s"))
    with pytest.raises(ValueError, match="gate"):
        manifest(
            scenario(
                "g",
                importance="gate",
                verification=["operational"],
                expectations=[],
            )
        )


def test_versions_recorded_and_unknown_key_rejected() -> None:
    m = manifest(scenario("s"))
    target = FakeTarget({"s": obs(revenue=100.0)})
    result = run_manifest(
        m, target, RunConfig(versions={"model": "m-1", "dataset": "snap-1"})
    )
    assert result.config.versions["model"] == "m-1"
    assert result.config.versions["corpus"] == "unspecified"
    with pytest.raises(ValueError, match="unknown version keys"):
        run_manifest(m, target, RunConfig(versions={"nope": "x"}))


def test_baseline_comparison_per_case_and_versions() -> None:
    m = manifest(scenario("a"), scenario("b"), scenario("c"))
    base = run_manifest(
        m,
        FakeTarget(
            {"a": obs(revenue=100.0), "b": obs(revenue=1.0), "c": obs(revenue=100.0)}
        ),
        RunConfig(versions={"model": "m-1", "dataset": "d1"}),
    )
    cur = run_manifest(
        m,
        FakeTarget(
            {"a": obs(revenue=1.0), "b": obs(revenue=100.0), "c": obs(revenue=100.2)}
        ),
        RunConfig(versions={"model": "m-2", "dataset": "d2"}),
    )
    cmp = compare_runs(base, cur)
    changes = {c.scenario_id: c.change for c in cmp.cases}
    assert changes == {"a": "regressed", "b": "improved", "c": "unchanged"}
    assert cmp.regressions == ("a",)
    assert {v.key for v in cmp.version_changes} == {"model", "dataset"}
    assert cmp.comparable is False  # dataset changed: drift, not just the agent
    c = next(x for x in cmp.cases if x.scenario_id == "c")
    assert c.numeric_deltas[0].delta == pytest.approx(0.2)


def test_serializer_refuses_sensitive_content(tmp_path: Path) -> None:
    m = manifest(scenario("s"))
    result = run_manifest(
        m,
        FakeTarget({"s": obs(revenue=100.0)}),
        RunConfig(versions={"model": "jane.doe@example.com"}),
    )
    with pytest.raises(SensitiveContentError):
        write_result(tmp_path / "r.json", result)
    assert not (tmp_path / "r.json").exists()


def test_cli_end_to_end_with_replay_and_baseline(tmp_path: Path) -> None:
    m = manifest(scenario("a"), scenario("b"))
    mpath = tmp_path / "manifest.json"
    mpath.write_text(m.model_dump_json())
    rec = tmp_path / "obs.json"
    rec.write_text(json.dumps({"a": {"values": {"revenue": 100.0}}}))
    out = tmp_path / "out" / "r1.json"
    args = ["run", "--manifest", str(mpath), "--observations", str(rec)]
    runner = CliRunner()
    first = runner.invoke(main, [*args, "--out", str(out), "--version", "model=m-1"])
    assert first.exit_code == 3, first.output  # b has no recording: blocked
    stored = load_result(out)
    assert isinstance(stored, RunResult)
    assert {c.scenario_id: c.status for c in stored.cases} == {
        "a": "passed",
        "b": "blocked",
    }
    second = runner.invoke(
        main, [*args, "--out", str(tmp_path / "r2.json"), "--baseline", str(out)]
    )
    assert "comparable" in second.output
    assert runner.invoke(main, ["summary", str(out)]).exit_code == 0
    assert runner.invoke(main, ["compare", str(out), str(out)]).exit_code == 0
    assert isinstance(ReplayTarget(rec), ReplayTarget)
