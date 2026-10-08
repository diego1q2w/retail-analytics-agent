"""Retrieval quality metrics. Pure functions over ranked example keys.

Conventions (stated in every report):

- Relevance is graded 0-2 (2: the method applies as written, 1: the method
  applies in part or with a definition caveat, 0: does not apply). Precision
  and recall treat grade >= 1 as relevant; nDCG uses the grades as gains.
- Only examples the asker is authorized to receive count as known relevant, so
  recall is relative to the *authorized* relevant set.
- Precision@k divides by the examples actually returned in the top k (the
  retriever may decline to fill k slots). A query that returns nothing has no
  precision; it is counted separately as a decline.
- Precision, recall, reciprocal rank and nDCG are defined only for questions
  with at least one authorized relevant example; the others (no match,
  unauthorized-only) are scored as no-match behavior and exposure.
- Reciprocal rank is 0 when no relevant example is returned.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def relevant_set(grades: Mapping[str, int]) -> frozenset[str]:
    return frozenset(key for key, grade in grades.items() if grade >= 1)


def precision_at_k(
    ranked: Sequence[str], relevant: frozenset[str], k: int
) -> float | None:
    """Share of the returned top-k that is relevant; None when nothing returned."""
    top = ranked[:k]
    if not top:
        return None
    return sum(key in relevant for key in top) / len(top)


def recall_at_k(
    ranked: Sequence[str], relevant: frozenset[str], k: int
) -> float | None:
    """Share of known relevant examples in the top k; None without any."""
    if not relevant:
        return None
    return len(relevant & set(ranked[:k])) / len(relevant)


def reciprocal_rank(ranked: Sequence[str], relevant: frozenset[str]) -> float | None:
    if not relevant:
        return None
    for position, key in enumerate(ranked, start=1):
        if key in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[str], grades: Mapping[str, int], k: int) -> float | None:
    """Normalized discounted cumulative gain with gain 2^grade - 1."""
    ideal = sorted((g for g in grades.values() if g > 0), reverse=True)[:k]
    if not ideal:
        return None

    def dcg(gains: Sequence[int]) -> float:
        return math.fsum(
            (2**gain - 1) / math.log2(position + 1)
            for position, gain in enumerate(gains, start=1)
        )

    got = [grades.get(key, 0) for key in ranked[:k]]
    return dcg(got) / dcg(ideal)


def mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None
