"""Compare a run with a baseline run: per-case changes and version differences."""

from __future__ import annotations

from typing import Final, Literal

from retail_analytics.application.contracts import ContractModel, Identifier
from retail_analytics.application.evaluation.results import (
    CaseResult,
    CaseStatus,
    RunResult,
)

COMPARISON_SCHEMA_VERSION: Final = 1

Change = Literal[
    "unchanged", "regressed", "improved", "coverage_changed", "new", "removed"
]
# Versions whose change means a difference may come from the world, not the agent.
DRIFT_KEYS: Final = ("dataset", "corpus", "metric_catalog")

_EXECUTED_OK: Final = "passed"
_BAD: Final = frozenset({"failed", "errored"})


class VersionDelta(ContractModel):
    key: str
    baseline: str
    current: str


class NumericDelta(ContractModel):
    check: Identifier
    baseline: float | None
    current: float | None
    delta: float | None


class CaseComparison(ContractModel):
    scenario_id: Identifier
    change: Change
    baseline_status: CaseStatus | None
    current_status: CaseStatus | None
    numeric_deltas: tuple[NumericDelta, ...] = ()


class Comparison(ContractModel):
    schema_version: Literal[1] = COMPARISON_SCHEMA_VERSION
    baseline_run_id: str
    current_run_id: str
    baseline_verdict_digest: str
    current_verdict_digest: str
    manifest_changed: bool
    version_changes: tuple[VersionDelta, ...]
    # False when data/corpus/metric versions or the manifest differ: case changes
    # may then reflect source drift rather than the agent.
    comparable: bool
    cases: tuple[CaseComparison, ...]
    regressions: tuple[Identifier, ...]
    improvements: tuple[Identifier, ...]


def _change(base: CaseResult | None, cur: CaseResult | None) -> Change:
    if base is None:
        return "new"
    if cur is None:
        return "removed"
    if base.status == cur.status:
        return "unchanged"
    if cur.status in _BAD and base.status not in _BAD:
        # passed->failed is a regression; passed->blocked/skipped is lost coverage.
        return "regressed"
    if base.status in _BAD and cur.status == _EXECUTED_OK:
        return "improved"
    return "coverage_changed"


def _numeric_deltas(
    base: CaseResult | None, cur: CaseResult | None
) -> tuple[NumericDelta, ...]:
    def observed(case: CaseResult | None) -> dict[str, float | None]:
        if case is None:
            return {}
        return {
            c.name: float(c.observed)
            if isinstance(c.observed, int | float) and not isinstance(c.observed, bool)
            else None
            for c in case.checks
            if c.kind == "numeric"
        }

    b, c = observed(base), observed(cur)
    deltas = []
    for name in sorted(set(b) | set(c)):
        bv, cv = b.get(name), c.get(name)
        delta = round(cv - bv, 9) if bv is not None and cv is not None else None
        if bv != cv:
            deltas.append(
                NumericDelta(check=name, baseline=bv, current=cv, delta=delta)
            )
    return tuple(deltas)


def compare_runs(baseline: RunResult, current: RunResult) -> Comparison:
    keys = sorted(set(baseline.config.versions) | set(current.config.versions))
    changes = tuple(
        VersionDelta(
            key=k,
            baseline=baseline.config.versions.get(k, "unspecified"),
            current=current.config.versions.get(k, "unspecified"),
        )
        for k in keys
        if baseline.config.versions.get(k) != current.config.versions.get(k)
    )
    manifest_changed = baseline.manifest.digest != current.manifest.digest
    base_cases = {c.scenario_id: c for c in baseline.cases}
    cur_cases = {c.scenario_id: c for c in current.cases}
    order = [c.scenario_id for c in current.cases] + sorted(
        set(base_cases) - set(cur_cases)
    )
    rows = []
    for sid in order:
        b, c = base_cases.get(sid), cur_cases.get(sid)
        rows.append(
            CaseComparison(
                scenario_id=sid,
                change=_change(b, c),
                baseline_status=b.status if b else None,
                current_status=c.status if c else None,
                numeric_deltas=_numeric_deltas(b, c),
            )
        )
    return Comparison(
        baseline_run_id=baseline.run_id,
        current_run_id=current.run_id,
        baseline_verdict_digest=baseline.verdict_digest,
        current_verdict_digest=current.verdict_digest,
        manifest_changed=manifest_changed,
        version_changes=changes,
        comparable=not manifest_changed
        and not any(v.key in DRIFT_KEYS for v in changes),
        cases=tuple(rows),
        regressions=tuple(r.scenario_id for r in rows if r.change == "regressed"),
        improvements=tuple(r.scenario_id for r in rows if r.change == "improved"),
    )
