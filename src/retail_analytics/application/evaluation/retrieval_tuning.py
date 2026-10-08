"""Threshold tuning on the tuning split only.

Sweeps the channel thresholds, scores each setting with the same metric code the
benchmark uses, and picks the best by one stated objective. The held-out split
is never read here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from retail_analytics.application.evaluation.retrieval_labels import LabeledQuestion
from retail_analytics.application.evaluation.retrieval_metrics import mean
from retail_analytics.application.evaluation.retrieval_target import (
    CorpusEntry,
    Retrieve,
    score_ranking,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.knowledge import ApplicabilityContext, ExampleRef
from retail_analytics.domain.retrieval import RetrievalConfig

MakeRetrieve = Callable[[RetrievalConfig], Retrieve]


@dataclass(frozen=True, slots=True)
class SweepRow:
    config: RetrievalConfig
    recall_at_3: float
    precision_at_3: float  # undefined precision counts as 0 for positives
    no_match_rate: float
    objective: float
    false_declines: int
    exposures: int


def objective(recall: float, precision: float, no_match: float) -> float:
    """Equal weight on finding relevant examples, their purity and declining."""
    return (recall + precision + no_match) / 3


async def sweep(
    make_retrieve: MakeRetrieve,
    configs: Sequence[RetrievalConfig],
    questions: Sequence[LabeledQuestion],
    scopes: Mapping[str, frozenset[str]],
    entries: Mapping[str, CorpusEntry],
    by_example: Mapping[str, str],
    context: ApplicabilityContext,
) -> list[SweepRow]:
    rows: list[SweepRow] = []
    for config in configs:
        retrieve = make_retrieve(config)
        recalls: list[float] = []
        precisions: list[float] = []
        declines = exposures = 0
        none_ok = none_total = 0
        for q in questions:
            ids = scopes[q.scope]
            result = await retrieve(ProductScope(ids, 1), context, q.text)
            ranked = tuple(_key(e.ref, by_example) for e in result.examples)
            obs = score_ranking(
                q,
                ranked,
                entries,
                ids,
                context.schema_version,
                context.metric_versions,
            )
            m = obs.measurements
            exposures += int(m["access_violations"])
            if q.expect == "none":
                none_total += 1
                none_ok += not ranked
            elif q.expect == "match":
                if "recall_at_3" in m:
                    recalls.append(m["recall_at_3"])
                    precisions.append(m.get("precision_at_3", 0.0))
                declines += not ranked
        recall = mean(recalls) or 0.0
        precision = mean(precisions) or 0.0
        no_match = none_ok / none_total if none_total else 1.0
        rows.append(
            SweepRow(
                config,
                recall,
                precision,
                no_match,
                objective(recall, precision, no_match),
                declines,
                exposures,
            )
        )
    return rows


def best(rows: Sequence[SweepRow]) -> SweepRow:
    """Highest objective; ties go to the stricter (higher) thresholds."""
    return max(
        rows,
        key=lambda r: (
            -r.exposures,
            r.objective,
            r.config.min_similarity,
            r.config.min_lexical_coverage,
        ),
    )


def _key(ref: ExampleRef, by_example: Mapping[str, str]) -> str:
    return by_example.get(ref.example_id, f"unknown:{ref.example_id}")
