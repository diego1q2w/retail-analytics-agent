from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import (
    Annotated,
    Any,
    Literal,
)

from pydantic import Field

from retail_analytics.application.contracts import (
    ContractModel,
    Identifier,
)

Mode = Literal["fixture", "live"]


Scalar = bool | int | float | str | None


class ScopeSpec(ContractModel):
    """Who is asking and what they may see (synthetic references only)."""

    executive_ref: Identifier
    product_scope: tuple[Identifier, ...] = ()


class Turn(ContractModel):
    """One user message. Agent turns are produced by the target, not authored."""

    text: Annotated[str, Field(min_length=1, max_length=8000)]


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


class JudgeScoreOut(ContractModel):
    dimension: Identifier
    score: float
    evidence_refs: tuple[Identifier, ...] = ()


@dataclass(frozen=True)
class EngineResult:
    row: Mapping[str, Any]
    bytes_processed: int = 0
    bytes_billed: int = 0
    job_id: str = ""
