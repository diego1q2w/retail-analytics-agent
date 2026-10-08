"""Readable text summaries of results and comparisons (no raw content exists)."""

from __future__ import annotations

from retail_analytics.application.evaluation.compare import Comparison
from retail_analytics.application.evaluation.results import Ratio, RunResult


def _ratio(r: Ratio) -> str:
    value = "n/a" if r.value is None else f"{r.value:.4f}"
    return f"{value} ({r.numerator}/{r.denominator})"


def render_summary(result: RunResult) -> str:
    agg = result.aggregates
    lines = [
        f"Run {result.run_id}  verdict: {result.verdict.upper()}",
        f"Manifest {result.manifest.manifest_id}@{result.manifest.manifest_version}"
        f"  mode={result.config.mode}  target={result.config.target_id}",
        "Versions: "
        + ", ".join(f"{k}={v}" for k, v in sorted(result.config.versions.items())),
        "Status: " + ", ".join(f"{k}={v}" for k, v in agg.status_counts.items()),
        "",
        "Deterministic",
        f"  numeric correctness : {_ratio(agg.numeric_correctness)}",
        f"  task completion     : {_ratio(agg.task_completion)}",
        f"  gate failures       : {_ratio(agg.safety_gate_failures)}",
    ]
    if agg.gate_failures:
        lines.append("  failed gates        : " + ", ".join(agg.gate_failures))
    lines += ["", "Judge scores (not gating)"]
    lines += [
        f"  {j.dimension}: mean={j.mean} samples={j.samples}" for j in agg.judge
    ] or ["  none"]
    lines += ["", "Operational measurements (not gating)"]
    lines += [
        f"  {m.name}: mean={m.mean} min={m.minimum} max={m.maximum} n={m.samples}"
        for m in agg.operational
    ] or ["  none"]
    lines += ["", "Cases"]
    for case in result.cases:
        note: str = case.reason or ""
        if case.blocked_on:
            note += " [" + ", ".join(case.blocked_on) + "]"
        if case.error_type:
            note += f" ({case.error_type})"
        lines.append(f"  {case.status.upper():8} {case.scenario_id}  {note}".rstrip())
        lines += [
            f"           - {c.name}: {c.detail}" for c in case.checks if not c.passed
        ]
    lines.append(f"\nverdict_digest {result.verdict_digest}")
    return "\n".join(lines) + "\n"


def render_comparison(comparison: Comparison) -> str:
    lines = [
        f"Baseline {comparison.baseline_run_id} -> current {comparison.current_run_id}",
        "comparable: "
        + (
            "yes" if comparison.comparable else "NO (data/corpus/metric/manifest drift)"
        ),
    ]
    lines += [
        f"  version {v.key}: {v.baseline} -> {v.current}"
        for v in comparison.version_changes
    ]
    lines.append(
        f"regressions: {', '.join(comparison.regressions) or 'none'}; "
        f"improvements: {', '.join(comparison.improvements) or 'none'}"
    )
    for row in comparison.cases:
        if row.change == "unchanged" and not row.numeric_deltas:
            continue
        lines.append(
            f"  {row.change:16} {row.scenario_id}: "
            f"{row.baseline_status} -> {row.current_status}"
        )
        lines += [
            f"           {d.check}: {d.baseline} -> {d.current} (delta {d.delta})"
            for d in row.numeric_deltas
        ]
    return "\n".join(lines) + "\n"
