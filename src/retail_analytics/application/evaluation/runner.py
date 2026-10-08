"""Run a manifest against a target and assemble a versioned result."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from retail_analytics.application.contracts.evaluation import (
    Mode,
    ScenarioInput,
    TargetObservation,
    TargetUnavailable,
)
from retail_analytics.application.evaluation.checks import evaluate_expectation
from retail_analytics.application.evaluation.manifest import (
    JudgeSpec,
    Manifest,
    Scenario,
)
from retail_analytics.application.evaluation.results import (
    UNSPECIFIED_VERSION,
    VERSION_KEYS,
    Aggregates,
    CaseResult,
    CaseStatus,
    CheckResult,
    JudgeAggregate,
    JudgeScore,
    ManifestRef,
    Measurement,
    MeasurementAggregate,
    ReasonCode,
    RunConfigRecord,
    RunResult,
    Verdict,
    canonical_json,
    deterministic_view,
    ratio,
    sha256_hex,
)
from retail_analytics.application.ports.evaluation import (
    EvaluationTarget,
    JudgeScorer,
)


@dataclass(frozen=True)
class RunConfig:
    """Everything about a run that is not the scenarios or the target's behavior."""

    mode: Mode = "fixture"
    # Environment capabilities present (names only, never credentials).
    available_capabilities: frozenset[str] = frozenset()
    # Component versions to record; keys must be in ``VERSION_KEYS``.
    versions: Mapping[str, str] = field(default_factory=dict)
    # Optional selection filters (recorded in the result).
    levels: frozenset[int] = frozenset()
    tags: frozenset[str] = frozenset()
    scenario_ids: frozenset[str] = frozenset()


def manifest_digest(manifest: Manifest) -> str:
    return sha256_hex(canonical_json(manifest.model_dump(mode="json")))


def _resolved_versions(versions: Mapping[str, str]) -> dict[str, str]:
    unknown = sorted(set(versions) - set(VERSION_KEYS))
    if unknown:
        raise ValueError(f"unknown version keys: {', '.join(unknown)}")
    return {key: versions.get(key, UNSPECIFIED_VERSION) for key in VERSION_KEYS}


def _selected(scenario: Scenario, config: RunConfig) -> bool:
    return (
        (not config.levels or scenario.level in config.levels)
        and (not config.tags or bool(config.tags & set(scenario.tags)))
        and (not config.scenario_ids or scenario.id in config.scenario_ids)
    )


def _case(scenario: Scenario, status: CaseStatus, **extra: object) -> CaseResult:
    return CaseResult(
        scenario_id=scenario.id,
        level=scenario.level,
        category=scenario.category,
        importance=scenario.importance,
        mode=scenario.mode,
        status=status,
        **extra,  # type: ignore[arg-type]
    )


def _pre_run_outcome(scenario: Scenario, config: RunConfig) -> CaseResult | None:
    if scenario.implementation_status != "implemented":
        return _case(scenario, "skipped", reason="not_implemented")
    if scenario.mode != config.mode:
        return _case(scenario, "skipped", reason="mode_excluded")
    missing = sorted(set(scenario.requires) - config.available_capabilities)
    if missing:
        return _case(
            scenario,
            "blocked",
            reason="missing_requirement",
            blocked_on=tuple(missing),
        )
    return None


def _score(
    spec: JudgeSpec,
    case_input: ScenarioInput,
    observation: TargetObservation,
    judges: Sequence[JudgeScorer],
) -> tuple[JudgeScore, ...]:
    scores: list[JudgeScore] = []
    for judge in judges:
        for out in judge.score(
            case_input,
            observation,
            spec.rubric_id,
            spec.dimensions,
        ):
            scores.append(
                JudgeScore(
                    judge_id=judge.judge_id,
                    rubric_id=spec.rubric_id,
                    dimension=out.dimension,
                    score=out.score,
                    evidence_refs=out.evidence_refs,
                )
            )
    return tuple(scores)


def run_scenario(
    scenario: Scenario,
    config: RunConfig,
    target: EvaluationTarget,
    judges: Sequence[JudgeScorer] = (),
) -> CaseResult:
    pre = _pre_run_outcome(scenario, config)
    if pre is not None:
        return pre
    case_input = ScenarioInput(
        scenario_id=scenario.id,
        mode=scenario.mode,
        fixture_ref=scenario.fixture_ref,
        scope=scenario.scope,
        dialogue=scenario.dialogue,
    )
    try:
        observation = target.run(case_input)
    except TargetUnavailable:
        return _case(scenario, "blocked", reason="target_unavailable")
    except Exception as exc:
        return _case(
            scenario, "errored", reason="target_error", error_type=type(exc).__name__
        )

    checks: tuple[CheckResult, ...] = tuple(
        evaluate_expectation(e, observation) for e in scenario.expectations
    )
    base: dict[str, object] = {
        "checks": checks,
        "sample_count": len(checks),
        "answer_digest": sha256_hex(observation.answer_text),
        "answer_chars": len(observation.answer_text),
        "measurements": tuple(
            Measurement(name=k, value=v)
            for k, v in sorted(observation.measurements.items())
        ),
    }
    status: CaseStatus = "passed" if checks else "scored"
    reason: ReasonCode | None = None
    if any(not c.passed for c in checks):
        status, reason = "failed", "checks_failed"
    if scenario.judge is not None:
        if not judges:
            if status != "failed":
                status, reason = "blocked", "judge_unavailable"
        else:
            try:
                base["judge_scores"] = _score(
                    scenario.judge, case_input, observation, judges
                )
            except Exception as exc:
                if status != "failed":
                    status, reason = "errored", "judge_error"
                    base["error_type"] = type(exc).__name__
    return _case(scenario, status, reason=reason, **base)


def _mean(values: Sequence[float]) -> float | None:
    return round(math.fsum(values) / len(values), 9) if values else None


def aggregate(cases: Sequence[CaseResult]) -> Aggregates:
    counts = Counter(c.status for c in cases)
    status_counts = {
        s: counts.get(s, 0)
        for s in ("passed", "failed", "errored", "skipped", "blocked", "scored")
    }
    checks = [c for case in cases for c in case.checks]
    numeric = [c for c in checks if c.kind == "numeric"]
    executed = [c for c in cases if c.status in ("passed", "failed", "errored")]
    gates = [c for c in executed if c.importance == "gate"]
    gate_failed = [c for c in gates if c.status != "passed"]

    judge_by_dim: dict[str, list[float]] = {}
    ops: dict[str, list[float]] = {}
    for case in cases:
        for score in case.judge_scores:
            judge_by_dim.setdefault(score.dimension, []).append(score.score)
        for m in case.measurements:
            ops.setdefault(m.name, []).append(m.value)
    return Aggregates(
        status_counts=status_counts,
        numeric_correctness=ratio(sum(c.passed for c in numeric), len(numeric)),
        task_completion=ratio(
            sum(c.status == "passed" for c in executed), len(executed)
        ),
        safety_gate_failures=ratio(len(gate_failed), len(gates)),
        gate_failures=tuple(c.scenario_id for c in gate_failed),
        judge=tuple(
            JudgeAggregate(dimension=d, samples=len(v), mean=_mean(v))
            for d, v in sorted(judge_by_dim.items())
        ),
        operational=tuple(
            MeasurementAggregate(
                name=n, samples=len(v), minimum=min(v), maximum=max(v), mean=_mean(v)
            )
            for n, v in sorted(ops.items())
        ),
    )


def verdict_of(cases: Sequence[CaseResult]) -> Verdict:
    """Failed beats incomplete; only an unblocked run with a pass is ``passed``."""
    statuses = {c.status for c in cases}
    if statuses & {"failed", "errored"}:
        return "failed"
    if "blocked" in statuses or "passed" not in statuses:
        return "incomplete"
    return "passed"


def run_manifest(
    manifest: Manifest,
    target: EvaluationTarget,
    config: RunConfig,
    judges: Sequence[JudgeScorer] = (),
    recorded_at: Callable[[], str] | None = None,
) -> RunResult:
    versions = _resolved_versions(config.versions)
    selected = [s for s in manifest.scenarios if _selected(s, config)]
    cases = tuple(run_scenario(s, config, target, judges) for s in selected)
    selection = {
        key: tuple(sorted(str(v) for v in values))
        for key, values in (
            ("levels", config.levels),
            ("tags", config.tags),
            ("scenario_ids", config.scenario_ids),
        )
        if values
    }
    record = RunConfigRecord(
        mode=config.mode,
        target_id=target.target_id,
        versions=versions,
        available_capabilities=tuple(sorted(config.available_capabilities)),
        judge_ids=tuple(sorted(j.judge_id for j in judges)),
        selection=selection,
    )
    digest = manifest_digest(manifest)
    verdict = verdict_of(cases)
    agg = aggregate(cases)
    verdict_digest = sha256_hex(
        canonical_json(
            {
                "manifest": digest,
                "config": record.model_dump(mode="json"),
                "cases": deterministic_view(cases),
                "verdict": verdict,
            }
        )
    )
    run_id = (
        "run-"
        + sha256_hex(
            canonical_json(
                {"manifest": digest, "config": record.model_dump(mode="json")}
            )
        )[:16]
    )
    return RunResult(
        run_id=run_id,
        manifest=ManifestRef(
            manifest_id=manifest.manifest_id,
            manifest_version=manifest.manifest_version,
            digest=digest,
        ),
        config=record,
        verdict=verdict,
        cases=cases,
        aggregates=agg,
        verdict_digest=verdict_digest,
        recorded_at=recorded_at() if recorded_at else None,
    )
