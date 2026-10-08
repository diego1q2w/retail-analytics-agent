from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.application.contracts.evaluation import (
    EngineResult,
    JudgeScoreOut,
    ScenarioInput,
    TargetObservation,
)


class EvaluationTarget(Protocol):
    target_id: str

    def run(self, case: ScenarioInput) -> TargetObservation: ...


class JudgeScorer(Protocol):
    """One judge model applying one versioned rubric to one evidence packet."""

    judge_id: str

    def score(
        self,
        case: ScenarioInput,
        observation: TargetObservation,
        rubric_id: str,
        dimensions: Sequence[str],
    ) -> Sequence[JudgeScoreOut]: ...


class SqlEngine(Protocol):
    """Runs one read-only statement that returns exactly one row."""

    def run_one(self, sql: str) -> EngineResult: ...
