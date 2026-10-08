"""Versioned scenario manifest (the input of an evaluation run).

A manifest is authored data: fixtures, scope, dialogue and expected values are
synthetic or sanitized and may be reviewed in version control. Results never
copy dialogue or expected text; see ``results``.
"""

from __future__ import annotations

import math
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from retail_analytics.application.contracts import ContractModel, Identifier
from retail_analytics.application.contracts.evaluation import (
    Mode,
    Scalar,
    ScopeSpec,
    Turn,
)

MANIFEST_SCHEMA_VERSION: Final = 1

Level = Literal[1, 2, 3]
Importance = Literal["gate", "threshold", "informational"]
ImplementationStatus = Literal["implemented", "planned", "deferred"]
VerificationMethod = Literal["deterministic", "judge", "operational"]

# Capabilities a scenario may need from the environment. A missing one blocks
# the scenario; it is never silently treated as a pass.
Requirement = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class NumericExpectation(ContractModel):
    """Match when ``|observed - expected| <= max(abs_tol, rel_tol * |expected|)``."""

    kind: Literal["numeric"] = "numeric"
    name: Identifier
    expected: float
    abs_tol: Annotated[float, Field(ge=0)] = 0.0
    rel_tol: Annotated[float, Field(ge=0)] = 0.0

    @model_validator(mode="after")
    def _finite(self) -> NumericExpectation:
        if not all(
            math.isfinite(v) for v in (self.expected, self.abs_tol, self.rel_tol)
        ):
            raise ValueError("numeric expectation values must be finite")
        return self


class ExactExpectation(ContractModel):
    """Observed value equals ``expected`` exactly (type included)."""

    kind: Literal["exact"] = "exact"
    name: Identifier
    expected: Scalar


class TextExpectation(ContractModel):
    """The answer text contains (or must not contain) a literal.

    Results keep only a digest of ``needle`` because it may be a canary for a
    restricted value.
    """

    kind: Literal["contains", "not_contains"]
    name: Identifier
    needle: Annotated[str, Field(min_length=1, max_length=2000)]


class ToolExpectation(ContractModel):
    """A tool was (not) invoked during the dialogue."""

    kind: Literal["tool_called", "tool_not_called"]
    name: Identifier
    tool: Identifier


Expectation = Annotated[
    NumericExpectation | ExactExpectation | TextExpectation | ToolExpectation,
    Field(discriminator="kind"),
]


class JudgeSpec(ContractModel):
    """Versioned rubric to score the answer against, per dimension."""

    rubric_id: Identifier
    dimensions: tuple[Identifier, ...] = Field(min_length=1)


class Scenario(ContractModel):
    id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=200)]
    level: Level
    category: Identifier
    mode: Mode = "fixture"
    importance: Importance = "threshold"
    implementation_status: ImplementationStatus = "implemented"
    verification: tuple[VerificationMethod, ...] = Field(min_length=1)
    fixture_ref: Identifier | None = None
    scope: ScopeSpec
    dialogue: tuple[Turn, ...] = Field(min_length=1)
    expectations: tuple[Expectation, ...] = ()
    judge: JudgeSpec | None = None
    requires: tuple[Requirement, ...] = ()
    tags: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        if len(set(self.verification)) != len(self.verification):
            raise ValueError("verification methods must be unique")
        names = [e.name for e in self.expectations]
        if len(set(names)) != len(names):
            raise ValueError("expectation names must be unique within a scenario")
        has_det = "deterministic" in self.verification
        if has_det != bool(self.expectations):
            raise ValueError(
                "deterministic verification requires expectations, and vice versa"
            )
        if ("judge" in self.verification) != (self.judge is not None):
            raise ValueError("judge verification requires a judge spec, and vice versa")
        if self.importance == "gate" and not has_det:
            raise ValueError("a gate scenario needs deterministic expectations")
        return self


class Manifest(ContractModel):
    schema_version: Literal[1] = MANIFEST_SCHEMA_VERSION
    manifest_id: Identifier
    # Bump when scenarios or expected values change; recorded in every result.
    manifest_version: Identifier
    scenarios: tuple[Scenario, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Manifest:
        ids = [s.id for s in self.scenarios]
        if len(set(ids)) != len(ids):
            raise ValueError("scenario ids must be unique")
        return self
