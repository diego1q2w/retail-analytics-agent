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

LEXICAL = "lexical"
SEMANTIC = "semantic"
CHANNELS = frozenset({LEXICAL, SEMANTIC})

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


# T36-F1 measured defaults (gemini-embedding-2, 768 dimensions; tuning split).
MEASURED_EMBEDDING_MODEL = "gemini-embedding-2"
MEASURED_EMBEDDING_DIMENSIONS = 768
SEMANTIC_MIN_SIMILARITY = 0.70
LEXICAL_MIN_COVERAGE = 0.75
SEMANTIC_WEIGHT = 2.0
# Original T24 placeholders. Kept for the offline hashing embedder, whose cosine
# scale is lexical-hash based and was never measured against the benchmark, and
# as the "before" row of the T36 comparison.
PLACEHOLDER_MIN_SIMILARITY = 0.55
PLACEHOLDER_MIN_LEXICAL_COVERAGE = 0.5


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Tunable retrieval settings.

    Defaults are the values measured for ``gemini-embedding-2`` (768 dimensions)
    on the T36 tuning split (T36-F1). Cosine scales differ by embedding model, so
    the offline hashing embedder gets its own thresholds (``HASHING_*``).
    """

    max_results: int = MAX_RESULTS_CEILING
    # Candidates kept per channel before fusion.
    channel_candidates: int = 10
    rrf_k: int = 60
    # A candidate must clear at least one channel's absolute threshold.
    min_similarity: float = SEMANTIC_MIN_SIMILARITY
    min_lexical_coverage: float = LEXICAL_MIN_COVERAGE
    # Weighted RRF: the semantic channel counts this many times the keyword
    # channel (keyword weight is 1). 1.0 is plain equal-weight RRF.
    semantic_weight: float = SEMANTIC_WEIGHT
    # Both channels in production. A single channel exists so evaluation can
    # compare keyword-only, semantic-only and fused ranking on one corpus.
    channels: frozenset[str] = CHANNELS

    def __post_init__(self) -> None:
        if not 1 <= self.max_results <= MAX_RESULTS_CEILING:
            raise ValueError(f"max_results must be 1-{MAX_RESULTS_CEILING}")
        if self.channel_candidates < self.max_results or self.rrf_k < 1:
            raise ValueError("invalid candidate counts")
        if not -1.0 <= self.min_similarity <= 1.0:
            raise ValueError("min_similarity must be within [-1, 1]")
        if not 0.0 <= self.min_lexical_coverage <= 1.0:
            raise ValueError("min_lexical_coverage must be within [0, 1]")
        if not 0.0 < self.semantic_weight <= 100.0:
            raise ValueError("semantic_weight must be within (0, 100]")
        if not self.channels or not self.channels <= CHANNELS:
            raise ValueError("channels must be a non-empty subset of lexical, semantic")

    @property
    def version(self) -> str:
        base = (
            f"rrf{self.rrf_k}-n{self.channel_candidates}-k{self.max_results}"
            f"-sim{self.min_similarity}-lex{self.min_lexical_coverage}"
            f"-ws{self.semantic_weight}"
        )
        if self.channels == CHANNELS:
            return base
        return f"{base}-only-{'+'.join(sorted(self.channels))}"


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
    rankings: Mapping[str, Sequence[str]],
    k: int,
    weights: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Fuse ranked lists by rank only; raw channel scores are incomparable.

    ``weights`` scales each channel's contribution (default 1 per channel).
    """
    fused: dict[str, float] = {}
    for channel, ranked in rankings.items():
        weight = 1.0 if weights is None else weights.get(channel, 1.0)
        for position, key in enumerate(ranked, start=1):
            fused[key] = fused.get(key, 0.0) + weight / (k + position)
    return fused
