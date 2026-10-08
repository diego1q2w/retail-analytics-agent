"""Versioned, machine-readable evaluation result format.

Stability rules (consumers: later evaluation tasks, baselines, release gates):

- ``schema_version`` changes only on a breaking change; new optional fields keep it.
- Deterministic sections (statuses, checks, counts, ``verdict_digest``) are
  identical for identical manifest, versions, mode and target behavior.
  Judge scores and operational measurements are separate sections, and
  ``recorded_at`` is the only wall-clock value (absent unless a clock is given).
- Nothing raw is stored: no dialogue, answer text, expected strings or exception
  messages. Strings appear as sha256 digests; error causes as type names.
- Every ratio states its denominator; ``value`` is null for a zero denominator.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Final, Literal

from pydantic import Field

from retail_analytics.application.contracts import ContractModel, Identifier
from retail_analytics.application.contracts.evaluation import Mode
from retail_analytics.application.evaluation.manifest import (
    Importance,
    Level,
)

RESULT_SCHEMA_VERSION: Final = 1

VERSION_KEYS: Final = (
    "code",
    "model",
    "config",
    "prompt",
    "persona",
    "metric_catalog",
    "policy",
    "dataset",
    "retrieval",
    "corpus",
)
UNSPECIFIED_VERSION: Final = "unspecified"

CaseStatus = Literal["passed", "failed", "errored", "skipped", "blocked", "scored"]
Verdict = Literal["passed", "failed", "incomplete"]
ReasonCode = Literal[
    "checks_failed",
    "not_implemented",
    "mode_excluded",
    "missing_requirement",
    "target_unavailable",
    "target_error",
    "judge_unavailable",
    "judge_error",
]
CheckKind = Literal[
    "numeric", "exact", "contains", "not_contains", "tool_called", "tool_not_called"
]
CheckDetail = Literal[
    "ok",
    "mismatch",
    "out_of_tolerance",
    "missing_observation",
    "wrong_type",
    "non_finite",
    "found",
    "not_found",
]


class SensitiveContentError(ValueError):
    """A result would contain something that looks like PII or a secret."""


class CheckResult(ContractModel):
    name: Identifier
    kind: CheckKind
    passed: bool
    detail: CheckDetail
    # Numbers and booleans only; strings are compared by digest.
    expected: float | bool | None = None
    observed: float | bool | None = None
    expected_digest: str | None = None
    observed_digest: str | None = None


class JudgeScore(ContractModel):
    judge_id: Identifier
    rubric_id: Identifier
    dimension: Identifier
    score: float
    evidence_refs: tuple[Identifier, ...] = ()


class Measurement(ContractModel):
    name: Identifier
    value: float


class CaseResult(ContractModel):
    scenario_id: Identifier
    level: Level
    category: Identifier
    importance: Importance
    mode: Mode
    status: CaseStatus
    reason: ReasonCode | None = None
    blocked_on: tuple[str, ...] = ()
    error_type: Identifier | None = None
    # Deterministic section.
    checks: tuple[CheckResult, ...] = ()
    sample_count: int = 0
    answer_digest: str | None = None
    answer_chars: int | None = None
    # Separate, non-gating sections.
    judge_scores: tuple[JudgeScore, ...] = ()
    measurements: tuple[Measurement, ...] = ()


class Ratio(ContractModel):
    numerator: int
    denominator: int
    value: float | None


class JudgeAggregate(ContractModel):
    dimension: Identifier
    samples: int
    mean: float | None


class MeasurementAggregate(ContractModel):
    name: Identifier
    samples: int
    minimum: float | None
    maximum: float | None
    mean: float | None


class Aggregates(ContractModel):
    status_counts: dict[str, int]
    numeric_correctness: Ratio
    task_completion: Ratio
    safety_gate_failures: Ratio
    gate_failures: tuple[Identifier, ...] = ()
    judge: tuple[JudgeAggregate, ...] = ()
    operational: tuple[MeasurementAggregate, ...] = ()


class ManifestRef(ContractModel):
    manifest_id: Identifier
    manifest_version: Identifier
    digest: str


class RunConfigRecord(ContractModel):
    mode: Mode
    target_id: str
    versions: dict[str, str]
    available_capabilities: tuple[str, ...]
    judge_ids: tuple[str, ...] = ()
    selection: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class RunResult(ContractModel):
    schema_version: Literal[1] = RESULT_SCHEMA_VERSION
    run_id: Annotated[str, Field(pattern=r"^run-[0-9a-f]{16}$")]
    manifest: ManifestRef
    config: RunConfigRecord
    verdict: Verdict
    cases: tuple[CaseResult, ...]
    aggregates: Aggregates
    # sha256 over the deterministic sections only.
    verdict_digest: str
    recorded_at: str | None = None


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ratio(numerator: int, denominator: int) -> Ratio:
    """Ratio with explicit counts; undefined (null) rather than 0 or 1 for 0/0."""
    value = None if denominator == 0 else numerator / denominator
    return Ratio(numerator=numerator, denominator=denominator, value=value)


def deterministic_view(cases: tuple[CaseResult, ...]) -> list[dict[str, object]]:
    """The part of each case that must not vary between identical runs."""
    return [
        c.model_dump(mode="json", exclude={"judge_scores", "measurements"})
        for c in cases
    ]


def canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


_SENSITIVE: Final = (
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("api key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")),
    ("google key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("phone number", re.compile(r"\+\d[\d ()-]{8,}\d")),
)


def assert_no_sensitive(text: str) -> None:
    """Fail closed if serialized output looks like PII or a credential."""
    for label, pattern in _SENSITIVE:
        if pattern.search(text):
            raise SensitiveContentError(f"result would contain a {label}")


def serialize_result(result: RunResult) -> str:
    """Canonical, readable JSON for storage; refuses sensitive-looking content."""
    text = json.dumps(
        result.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=True
    )
    assert_no_sensitive(text)
    return text + "\n"
