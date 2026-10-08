"""Held-out reference conversations: split, coverage and runner behavior.

The agent target does not exist yet, so these tests drive the manifest with a
replay target built from the expectations themselves (a known-good agent) and
with deliberately wrong variants that the runner must reject.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from click.testing import CliRunner

from retail_analytics.adapters.evaluation.files import load_manifest
from retail_analytics.application.evaluation.manifest import (
    ExactExpectation,
    Expectation,
    Manifest,
    NumericExpectation,
    Scenario,
    TextExpectation,
    ToolExpectation,
)
from retail_analytics.application.evaluation.ports import (
    JudgeScoreOut,
    ScenarioInput,
    TargetObservation,
)
from retail_analytics.application.evaluation.results import RunResult
from retail_analytics.application.evaluation.runner import (
    RunConfig,
    run_manifest,
    run_scenario,
)
from retail_analytics.application.golden_seed_library import seed_library
from retail_analytics.bootstrap.evaluate import main
from tests import golden_seed_fixture as seed_fx
from tests import heldout_fixture as fx

MANIFEST = load_manifest(fx.MANIFEST_PATH)
SPLITS = json.loads(fx.SPLITS_PATH.read_text(encoding="utf-8"))
AGENT = frozenset({"agent_runtime"})


class RubricJudge:
    judge_id = "stub-judge"

    def score(
        self,
        case: ScenarioInput,
        observation: TargetObservation,
        rubric_id: str,
        dimensions: Sequence[str],
    ) -> Sequence[JudgeScoreOut]:
        return [JudgeScoreOut(dimension=d, score=1.0) for d in dimensions]


class KnownGoodAgent:
    """Answers every scenario exactly as its expectations require."""

    target_id = "known-good"

    def __init__(self, manifest: Manifest) -> None:
        self.by_id = {s.id: s for s in manifest.scenarios}

    def run(self, case: ScenarioInput) -> TargetObservation:
        return observation_for(self.by_id[case.scenario_id].expectations)


class FixedAgent:
    """Returns one canned observation whatever it is asked."""

    target_id = "fixed"

    def __init__(self, observation: TargetObservation) -> None:
        self.observation = observation

    def run(self, case: ScenarioInput) -> TargetObservation:
        return self.observation


def observation_for(
    expectations: Sequence[Expectation], wrong: str | None = None
) -> TargetObservation:
    """Build an observation satisfying all expectations, except ``wrong``."""
    values: dict[str, float | str | bool] = {}
    text: list[str] = []
    tools: list[str] = []
    for exp in expectations:
        broken = exp.name == wrong
        if isinstance(exp, NumericExpectation):
            values[exp.name] = exp.expected + (1000.0 if broken else 0.0)
        elif isinstance(exp, ExactExpectation):
            good = exp.expected
            if broken:
                bad: float | str | bool | None = (
                    (not good)
                    if isinstance(good, bool)
                    else f"{good}-wrong"
                    if isinstance(good, str)
                    else 12345
                )
                assert bad is not None
                values[exp.name] = bad
            else:
                assert good is not None
                values[exp.name] = good
        elif isinstance(exp, TextExpectation):
            present = (exp.kind == "contains") != broken
            if present:
                text.append(exp.needle)
        elif isinstance(exp, ToolExpectation) and (exp.kind == "tool_called") != broken:
            tools.append(exp.tool)
    return TargetObservation(
        answer_text=" ".join(text), values=values, tool_calls=tuple(tools)
    )


# ------------------------------------------------------------------ split


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def test_manifest_and_split_file_agree_on_explicit_identifiers() -> None:
    held = SPLITS["heldout"]
    ids = [s.id for s in MANIFEST.scenarios]
    assert held["manifest_id"] == MANIFEST.manifest_id
    assert held["fixture_id"] == fx.FIXTURE_ID
    assert held["scenario_ids"] == ids
    assert all(i.startswith(held["id_prefix"]) for i in ids)
    for scenario in MANIFEST.scenarios:
        assert scenario.fixture_ref == fx.FIXTURE_ID
        assert "heldout" in scenario.tags


def test_golden_seed_material_stays_out_of_the_heldout_set() -> None:
    assert SPLITS["training"]["golden_seed_fixture_id"] == seed_fx.FIXTURE_ID
    assert seed_fx.FIXTURE_ID != fx.FIXTURE_ID
    seeds = seed_library()
    seed_words = [words(s.question) for s in seeds]
    seed_keys = {s.key for s in seeds}
    for scenario in MANIFEST.scenarios:
        assert not ({scenario.id, *scenario.tags} & seed_keys)
        for turn in scenario.dialogue:
            asked = words(turn.text)
            for other in seed_words:
                overlap = len(asked & other) / len(asked | other)
                assert overlap < 0.6, (scenario.id, turn.text)
    seed_names = {p.name for p in seed_fx.PRODUCTS}
    assert not seed_names & {p.name for p in fx.products()}
    assert not {p.product_id for p in seed_fx.PRODUCTS} & {
        p.product_id for p in fx.products()
    }


def test_no_heldout_text_leaks_into_seed_reports() -> None:
    needles = [
        e.needle
        for s in MANIFEST.scenarios
        for e in s.expectations
        if isinstance(e, TextExpectation) and e.kind == "contains"
    ]
    for seed in seed_library():
        for needle in needles:
            assert needle not in seed.report_body, (seed.key, needle)


# --------------------------------------------------------------- coverage


def test_conversations_cover_analytics_privacy_and_access() -> None:
    categories = {s.category for s in MANIFEST.scenarios}
    assert {
        "scope_calculation",
        "calculation",
        "dates",
        "definitions",
        "demographics",
        "empty_results",
        "products",
        "schema",
        "multi_step",
        "reports",
        "sparse_periods",
        "customers",
        "corrections",
        "access",
        "privacy",
        "adversarial",
    } <= categories
    tags = {t for s in MANIFEST.scenarios for t in s.tags}
    assert {
        "wrong_scope",
        "empty_scope",
        "pii",
        "mixed_order",
        "distinct_count",
    } <= tags
    assert {s.level for s in MANIFEST.scenarios} == {1, 2, 3}
    scopes = {s.scope.executive_ref: s.scope.product_scope for s in MANIFEST.scenarios}
    assert scopes["ho-exec-empty"] == ()
    assert len(scopes) == 5


def test_security_scenarios_are_release_gates_with_canaries() -> None:
    for scenario in MANIFEST.scenarios:
        if scenario.category in {"access", "privacy", "adversarial"}:
            assert scenario.importance == "gate", scenario.id
            assert any(
                isinstance(e, TextExpectation) and e.kind == "not_contains"
                for e in scenario.expectations
            ) or any(
                isinstance(e, ExactExpectation) and e.expected is False
                for e in scenario.expectations
            )


def test_conversation_expectations_name_definition_contributors_actions_evidence() -> (
    None
):
    names = {e.name for s in MANIFEST.scenarios for e in s.expectations}
    assert {
        "definition_disclosed",
        "contributors_listed",
        "unsupported_causal_claim",
        "action_items_present",
        "evidence_cited",
        "scope_disclosed",
        "partial_period_labeled",
        "empty_result_explained",
        "sample_size_caveated",
    } <= names
    report = next(s for s in MANIFEST.scenarios if "contributors" in s.tags)
    assert report.judge is not None
    assert {"definition_disclosure", "contributors_not_causes", "action_items"} <= set(
        report.judge.dimensions
    )
    assert len(report.dialogue) == 3


def test_scenario_input_hides_expected_values() -> None:
    case = ScenarioInput(
        scenario_id="x",
        mode="fixture",
        fixture_ref=fx.FIXTURE_ID,
        scope=MANIFEST.scenarios[0].scope,
        dialogue=MANIFEST.scenarios[0].dialogue,
    )
    assert "649.35" not in case.model_dump_json()


# ----------------------------------------------------------------- runner


def run_known_good() -> RunResult:
    return run_manifest(
        MANIFEST,
        KnownGoodAgent(MANIFEST),
        RunConfig(available_capabilities=AGENT),
        judges=[RubricJudge()],
    )


def test_known_good_agent_passes_every_scenario() -> None:
    result = run_known_good()
    statuses = {c.scenario_id: c.status for c in result.cases}
    assert set(statuses.values()) <= {"passed"}, statuses
    assert result.verdict == "passed"
    assert result.aggregates.safety_gate_failures.value == 0


def test_every_single_wrong_answer_fails_its_scenario() -> None:
    checked = 0
    for scenario in MANIFEST.scenarios:
        for exp in scenario.expectations:
            if isinstance(exp, ToolExpectation):
                continue
            observation = observation_for(scenario.expectations, wrong=exp.name)

            case = run_scenario(
                scenario,
                RunConfig(available_capabilities=AGENT),
                FixedAgent(observation),
                judges=[RubricJudge()],
            )
            assert case.status == "failed", (scenario.id, exp.name)
            failing = {c.name for c in case.checks if not c.passed}
            assert exp.name in failing, (scenario.id, exp.name, failing)
            checked += 1
    assert checked > 100


def test_plausible_wrong_reports_are_rejected() -> None:
    """Realistic mistakes, not just arbitrary perturbation."""

    def fails(sid: str, **changes: float | str | bool) -> bool:
        scenario = next(s for s in MANIFEST.scenarios if s.id == sid)
        base = observation_for(scenario.expectations)
        observation = base.model_copy(update={"values": {**base.values, **changes}})

        case = run_scenario(
            scenario,
            RunConfig(available_capabilities=AGENT),
            FixedAgent(observation),
            judges=[RubricJudge()],
        )
        return case.status == "failed"

    # Unscoped totals reported to the scoped executive.
    assert fails("ho-l1-revenue-north-sept", revenue=839.35)
    # Whole-basket average instead of permitted items only.
    assert fails("ho-l1-mixed-order-visible-only", completed_amount=759.35)
    # Summed per-brand distinct customers.
    assert fails("ho-l1-distinct-customers-not-additive", either_customers=16)
    # Boundary row at 2026-09-01 00:00:00 leaking into August.
    assert fails("ho-l1-month-boundary-august", revenue=992.85, completed_items=14)
    # Counting returned items as revenue.
    assert fails("ho-l1-status-mix-september", revenue=989.35)
    # A "zero" that should have been a different result is not accepted as tolerance.
    assert fails("ho-l1-empty-result-explained", revenue=35.0)
    # Including the partial current day.
    assert fails("ho-l1-partial-month-comparable", month_to_date_revenue=584.99)


def test_leaking_a_canary_fails_the_scenario() -> None:
    scenario = next(s for s in MANIFEST.scenarios if s.id == "ho-l1-pii-names-emails")
    good = observation_for(scenario.expectations)
    leaked = good.model_copy(
        update={"answer_text": "Top spender: person5007@example.invalid"}
    )

    case = run_scenario(
        scenario, RunConfig(available_capabilities=AGENT), FixedAgent(leaked)
    )
    assert case.status == "failed"
    assert {c.name for c in case.checks if not c.passed} == {"no_canary_email"}


def test_without_an_agent_everything_is_blocked_not_passed(tmp_path: Path) -> None:
    result = run_manifest(MANIFEST, KnownGoodAgent(MANIFEST), RunConfig())
    assert {c.status for c in result.cases} == {"blocked"}
    assert all(c.blocked_on == ("agent_runtime",) for c in result.cases)
    assert result.verdict == "incomplete"

    recordings = tmp_path / "none.json"
    recordings.write_text("{}", encoding="utf-8")
    ran = CliRunner().invoke(
        main,
        [
            "run",
            "--manifest",
            str(fx.MANIFEST_PATH),
            "--target",
            "replay",
            "--observations",
            str(recordings),
            "--out",
            str(tmp_path / "result.json"),
        ],
    )
    assert ran.exit_code == 3, ran.output


def test_replay_cli_without_recordings_is_incomplete_even_with_the_capability(
    tmp_path: Path,
) -> None:
    recordings = tmp_path / "obs.json"
    recordings.write_text("{}", encoding="utf-8")
    ran = CliRunner().invoke(
        main,
        [
            "run",
            "--manifest",
            str(fx.MANIFEST_PATH),
            "--target",
            "replay",
            "--observations",
            str(recordings),
            "--capability",
            "agent_runtime",
            "--out",
            str(tmp_path / "r.json"),
        ],
    )
    assert ran.exit_code == 3, ran.output


def test_replay_cli_passes_a_known_good_recording_and_fails_a_bad_one(
    tmp_path: Path,
) -> None:
    good: dict[str, object] = {}
    bad: dict[str, object] = {}
    for scenario in MANIFEST.scenarios:
        good[scenario.id] = observation_for(scenario.expectations).model_dump(
            mode="json"
        )
        bad[scenario.id] = observation_for(
            scenario.expectations, wrong=scenario.expectations[0].name
        ).model_dump(mode="json")
    runner = CliRunner()

    def run(name: str, recording: dict[str, object], *extra: str) -> int:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(recording), encoding="utf-8")
        result = runner.invoke(
            main,
            [
                "run",
                "--manifest",
                str(fx.MANIFEST_PATH),
                "--target",
                "replay",
                "--observations",
                str(path),
                "--capability",
                "agent_runtime",
                "--out",
                str(tmp_path / f"{name}-result.json"),
                *extra,
            ],
        )
        return result.exit_code

    assert run("bad", bad) == 1
    # Without a judge the judged scenarios are blocked, so the verdict is
    # incomplete (3) for a good agent, never a silent pass.
    assert run("good", good) == 3


def test_scenario_validates_with_runner_schema() -> None:
    for scenario in MANIFEST.scenarios:
        assert isinstance(scenario, Scenario)
        assert scenario.requires == ("agent_runtime",)
        assert scenario.implementation_status == "implemented"
