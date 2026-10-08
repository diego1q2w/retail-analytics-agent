"""Pure ranking rules for hybrid Golden retrieval: keyword, vector and fusion.

Nothing here knows about access: callers pass only entries that are already
eligible, so scores and corpus statistics never reflect restricted content.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Golden retrieval never supplies more than this many examples (design §41).
MAX_RESULTS_CEILING = 3

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        *("a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does"),
        *("for", "from", "how", "i", "in", "is", "it", "its", "me", "my", "of"),
        *("on", "or", "our", "show", "tell", "that", "the", "their", "this", "to"),
        *("us", "was", "we", "were", "what", "when", "which", "who", "why"),
        *("with", "you", "your"),
    }
)


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Tunable retrieval settings; defaults are placeholders until T36 measures."""

    max_results: int = MAX_RESULTS_CEILING
    # Candidates kept per channel before fusion.
    channel_candidates: int = 10
    rrf_k: int = 60
    # A candidate must clear at least one channel's absolute threshold.
    min_similarity: float = 0.55
    min_lexical_coverage: float = 0.5

    def __post_init__(self) -> None:
        if not 1 <= self.max_results <= MAX_RESULTS_CEILING:
            raise ValueError(f"max_results must be 1-{MAX_RESULTS_CEILING}")
        if self.channel_candidates < self.max_results or self.rrf_k < 1:
            raise ValueError("invalid candidate counts")
        if not -1.0 <= self.min_similarity <= 1.0:
            raise ValueError("min_similarity must be within [-1, 1]")
        if not 0.0 <= self.min_lexical_coverage <= 1.0:
            raise ValueError("min_lexical_coverage must be within [0, 1]")

    @property
    def version(self) -> str:
        return (
            f"rrf{self.rrf_k}-n{self.channel_candidates}-k{self.max_results}"
            f"-sim{self.min_similarity}-lex{self.min_lexical_coverage}"
        )


def tokenize(text: str) -> tuple[str, ...]:
    """Lowercase content words with a light plural fold (no stemming library)."""
    out: list[str] = []
    for word in _TOKEN.findall(text.lower()):
        if word in _STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class LexicalScore:
    score: float
    coverage: float  # share of distinct query terms found in the document


def bm25(
    query: Sequence[str],
    documents: Mapping[str, Sequence[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> dict[str, LexicalScore]:
    """BM25 over ``documents`` only (statistics come from this set alone)."""
    terms = set(query)
    if not terms or not documents:
        return {}
    count = len(documents)
    average = sum(len(d) for d in documents.values()) / count or 1.0
    frequency = {t: sum(1 for d in documents.values() if t in d) for t in terms}
    scores: dict[str, LexicalScore] = {}
    for key, tokens in documents.items():
        tf = Counter(tokens)
        total = 0.0
        matched = 0
        for term in terms:
            if tf[term] == 0:
                continue
            matched += 1
            idf = math.log(
                1 + (count - frequency[term] + 0.5) / (frequency[term] + 0.5)
            )
            norm = (
                tf[term]
                * (k1 + 1)
                / (tf[term] + k1 * (1 - b + b * len(tokens) / average))
            )
            total += idf * norm
        scores[key] = LexicalScore(total, matched / len(terms))
    return scores


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vector dimensions differ")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


def rank(scores: Mapping[str, float], limit: int) -> list[str]:
    """Best first; ties break on key so results are reproducible."""
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [key for key, _ in ordered[:limit]]


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]], k: int
) -> dict[str, float]:
    """Fuse ranked lists by rank only; raw channel scores are incomparable."""
    fused: dict[str, float] = {}
    for ranked in rankings.values():
        for position, key in enumerate(ranked, start=1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + position)
    return fused
