"""Aggregate per-question retrieval measurements into a comparison report.

Reads only what the runner stored (per-case measurements, statuses) plus the
label file for categories and splits, so the report can be rebuilt from result
files alone. Every mean states its denominator, and a seeded bootstrap interval
shows how little a small question set can pin down.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from retail_analytics.application.evaluation.results import RunResult
from retail_analytics.application.evaluation.retrieval_labels import (
    LabeledQuestion,
    LabelSet,
)
from retail_analytics.application.evaluation.retrieval_metrics import mean

RANKING_METRICS = (
    "precision_at_1",
    "precision_at_3",
    "recall_at_1",
    "recall_at_3",
    "reciprocal_rank",
    "ndcg_at_3",
)
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261008


@dataclass(frozen=True, slots=True)
class Estimate:
    n: int
    mean: float | None
    low: float | None
    high: float | None


def estimate(values: Sequence[float]) -> Estimate:
    """Mean with a 95% percentile bootstrap interval (seeded, reproducible)."""
    if not values:
        return Estimate(0, None, None, None)
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - resampling only
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    low = means[int(0.025 * BOOTSTRAP_RESAMPLES)]
    high = means[int(0.975 * BOOTSTRAP_RESAMPLES) - 1]
    return Estimate(n, mean(values), low, high)


@dataclass(frozen=True, slots=True)
class VariantSummary:
    variant: str
    split: str
    questions: int  # executed (not blocked) questions in the split
    blocked: int
    ranking: Mapping[str, Estimate]
    no_match_correct: tuple[int, int]  # correct, questions expecting none
    false_declines: tuple[int, int]  # declined, questions expecting a match
    returned_per_question: Estimate
    access_violations: int
    exposed_questions: int


def _measure(case_measurements: Mapping[str, float], name: str) -> float | None:
    return case_measurements.get(name)


def summarize(
    variant: str,
    result: RunResult,
    labels: LabelSet,
    split: str,
    *,
    categories: frozenset[str] | None = None,
) -> VariantSummary:
    questions: Mapping[str, LabeledQuestion] = {q.id: q for q in labels.questions}
    chosen = [
        c
        for c in result.cases
        if (q := questions.get(c.scenario_id)) is not None
        and q.split == split
        and (categories is None or q.category in categories)
    ]
    executed = [c for c in chosen if c.status in ("passed", "failed")]
    per: dict[str, list[float]] = {name: [] for name in RANKING_METRICS}
    none_total = none_ok = match_total = declined = 0
    returned: list[float] = []
    violations = exposed = 0
    for case in executed:
        m = {x.name: x.value for x in case.measurements}
        q = questions[case.scenario_id]
        count = m.get("returned_count", 0.0)
        returned.append(count)
        bad = int(m.get("access_violations", 0.0))
        violations += bad
        exposed += bad > 0
        for name in RANKING_METRICS:
            value = _measure(m, name)
            if value is not None:
                per[name].append(value)
        if q.expect == "none":
            none_total += 1
            none_ok += count == 0
        elif q.expect == "match":
            match_total += 1
            declined += count == 0
    return VariantSummary(
        variant=variant,
        split=split,
        questions=len(executed),
        blocked=len(chosen) - len(executed),
        ranking={name: estimate(values) for name, values in per.items()},
        no_match_correct=(none_ok, none_total),
        false_declines=(declined, match_total),
        returned_per_question=estimate(returned),
        access_violations=violations,
        exposed_questions=exposed,
    )


def _cell(e: Estimate) -> str:
    if e.mean is None or e.low is None or e.high is None:
        return "n/a (n=0)"
    return f"{e.mean:.2f} [{e.low:.2f}-{e.high:.2f}] n={e.n}"


def _short(e: Estimate) -> str:
    return "n/a" if e.mean is None else f"{e.mean:.2f} (n={e.n})"


def render(summaries: Sequence[VariantSummary]) -> str:
    """Markdown table: one row per variant."""
    header = (
        "| variant | split | P@1 | P@3 | R@3 | MRR | nDCG@3 | no-match correct "
        "| false declines | access violations |"
    )
    lines = [header, "|" + "---|" * 10]
    for s in summaries:
        r = s.ranking
        nm = f"{s.no_match_correct[0]}/{s.no_match_correct[1]}"
        fd = f"{s.false_declines[0]}/{s.false_declines[1]}"
        lines.append(
            f"| {s.variant} | {s.split} | {_cell(r['precision_at_1'])} "
            f"| {_cell(r['precision_at_3'])} | {_cell(r['recall_at_3'])} "
            f"| {_cell(r['reciprocal_rank'])} | {_cell(r['ndcg_at_3'])} "
            f"| {nm} | {fd} | {s.access_violations} in "
            f"{s.exposed_questions}/{s.questions} questions |"
        )
    return "\n".join(lines) + "\n"


def render_categories(
    variant: str, result: RunResult, labels: LabelSet, split: str
) -> str:
    """Per-category breakdown for one variant (small denominators: read with care)."""
    lines = [
        f"{variant} on {split}, by question category",
        "| category | questions | R@3 | MRR | returned per question "
        "| no-match correct | false declines | access violations |",
        "|" + "---|" * 8,
    ]
    for category in sorted({q.category for q in labels.split(split)}):  # type: ignore[arg-type]
        s = summarize(variant, result, labels, split, categories=frozenset({category}))
        r = s.ranking
        recall = _short(r["recall_at_3"])
        rr = _short(r["reciprocal_rank"])
        lines.append(
            f"| {category} | {s.questions} "
            f"| {recall} | {rr} "
            f"| {s.returned_per_question.mean or 0:.2f} "
            f"| {s.no_match_correct[0]}/{s.no_match_correct[1]} "
            f"| {s.false_declines[0]}/{s.false_declines[1]} | {s.access_violations} |"
        )
    return "\n".join(lines) + "\n"
