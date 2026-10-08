"""Ports the runner needs. The agent under test is just one implementation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from retail_analytics.application.contracts import ContractModel, Identifier
from retail_analytics.application.evaluation.manifest import (
    Mode,
    Scalar,
    ScopeSpec,
    Turn,
)


class TargetUnavailable(Exception):
    """The target cannot run this case now (credentials, provider, no recording).

    Mapped to a blocked case, never to a pass or a failure of the agent.
    """


class ScenarioInput(ContractModel):
    """What a target sees: no expectations, so it cannot grade itself."""

    scenario_id: Identifier
    mode: Mode
    fixture_ref: Identifier | None
    scope: ScopeSpec
    dialogue: tuple[Turn, ...]


class TargetObservation(ContractModel):
    """What a target reports. Only digests and numbers reach the result file.

    ``values`` holds named intermediate and final results (check names refer to
    them); ``measurements`` holds operational numbers such as latency, model
    usage, query count and bytes processed.
    """

    answer_text: str = ""
    values: Mapping[Identifier, Scalar] = {}
    tool_calls: tuple[Identifier, ...] = ()
    measurements: Mapping[Identifier, float] = {}


class EvaluationTarget(Protocol):
    target_id: str

    def run(self, case: ScenarioInput) -> TargetObservation: ...


class JudgeScoreOut(ContractModel):
    dimension: Identifier
    score: float
    evidence_refs: tuple[Identifier, ...] = ()


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
