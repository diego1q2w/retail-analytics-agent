"""Labeled retrieval evaluation data: corpus, questions, relevance judgments.

The corpus is the ten project-authored seeds plus a few evaluation-only
examples (restricted, incompatible, retired and eligible distractors). Labels
say which examples a *method-wise* answer needs, not which share words with the
question. Everything here is synthetic.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from pydantic import Field, model_validator

from retail_analytics.application.contracts import ContractModel, Identifier
from retail_analytics.application.evaluation.retrieval_metrics import relevant_set
from retail_analytics.domain.retrieval import tokenize

Split = Literal["tuning", "heldout"]
Category = Literal[
    "paraphrase",
    "direct",
    "ambiguous",
    "incompatible",
    "retired",
    "unauthorized",
    "authorized_restricted",
    "no_match",
]
# match: relevant examples exist; none: a correct answer returns no example;
# either: ambiguous, returning nothing is acceptable.
Expect = Literal["match", "none", "either"]
FinalStatus = Literal["published", "retired"]

# Questions of different splits must differ more than this (Jaccard over
# content words); paraphrases of one intent always share a split.
LEAKAGE_JACCARD = 0.5


class EvalExample(ContractModel):
    """An evaluation-only example added to the seed library."""

    key: Identifier
    role: str
    question: Annotated[str, Field(min_length=1)]
    method_summary: Annotated[str, Field(min_length=1)]
    sql: Annotated[str, Field(min_length=1)]
    report_markdown: Annotated[str, Field(min_length=1)]
    schema_version: str
    metrics: tuple[tuple[str, int], ...] = ()
    restricted_products: tuple[str, ...] = ()
    final_status: FinalStatus = "published"

    @property
    def example_id(self) -> str:
        return hashlib.sha256(f"retrieval-eval:{self.key}".encode()).hexdigest()[:32]


class EvalCorpus(ContractModel):
    corpus_version: Identifier
    note: str
    revision: int
    examples: tuple[EvalExample, ...]

    @model_validator(mode="after")
    def _unique(self) -> EvalCorpus:
        keys = [e.key for e in self.examples]
        if len(set(keys)) != len(keys):
            raise ValueError("example keys must be unique")
        return self


class LabeledQuestion(ContractModel):
    id: Identifier
    split: Split
    group: Identifier
    category: Category
    scope: Identifier
    expect: Expect
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    # example key -> grade 1 or 2 (absent means 0)
    relevant: Mapping[str, Annotated[int, Field(ge=1, le=2)]] = {}
    # Examples that must never be returned to this asker, by hand.
    forbidden: tuple[str, ...] = ()


class Context(ContractModel):
    schema_version: str


class LabelSet(ContractModel):
    labels_version: Identifier
    labeling_notes: str
    context: Context
    scopes: Mapping[Identifier, tuple[str, ...]]
    questions: tuple[LabeledQuestion, ...]

    @model_validator(mode="after")
    def _consistent(self) -> LabelSet:
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question ids must be unique")
        for q in self.questions:
            if q.scope not in self.scopes:
                raise ValueError(f"{q.id}: unknown scope {q.scope}")
        return self

    def split(self, name: Split) -> tuple[LabeledQuestion, ...]:
        return tuple(q for q in self.questions if q.split == name)


def content_words(text: str) -> frozenset[str]:
    return frozenset(tokenize(text)) or frozenset(
        re.findall(r"[a-z0-9]+", text.lower())
    )


def jaccard(a: str, b: str) -> float:
    left, right = content_words(a), content_words(b)
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def validate_labels(
    labels: LabelSet,
    corpus_keys: frozenset[str],
    corpus_questions: Mapping[str, str],
    seed_questions: Sequence[str],
) -> list[str]:
    """Problems that would make scores untrustworthy; empty when sound."""
    problems: list[str] = []
    split_of: dict[str, str] = {}
    for q in labels.questions:
        if split_of.setdefault(q.group, q.split) != q.split:
            problems.append(f"group {q.group} spans splits")
        unknown = (set(q.relevant) | set(q.forbidden)) - corpus_keys
        if unknown:
            problems.append(f"{q.id}: unknown examples {sorted(unknown)}")
        if set(q.relevant) & set(q.forbidden):
            problems.append(f"{q.id}: example both relevant and forbidden")
        if q.expect == "match" and not q.relevant:
            problems.append(f"{q.id}: match question without a relevant example")
        if q.expect == "none" and relevant_set(q.relevant) and q.category == "no_match":
            problems.append(f"{q.id}: no-match question with relevant examples")
        if q.category == "no_match" and q.expect != "none":
            problems.append(f"{q.id}: no_match must expect none")
    tuning = labels.split("tuning")
    heldout = labels.split("heldout")
    for held in heldout:
        for other in tuning:
            if jaccard(held.text, other.text) >= LEAKAGE_JACCARD:
                problems.append(f"leakage {held.id} ~ {other.id}")
        for text in (*seed_questions, *corpus_questions.values()):
            if jaccard(held.text, text) >= LEAKAGE_JACCARD:
                problems.append(f"{held.id} repeats a corpus question")
    return problems
