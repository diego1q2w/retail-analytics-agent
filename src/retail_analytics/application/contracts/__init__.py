"""Shared base for versioned wire contracts (tool calls/results, progress events).

Contracts are strict: unknown fields are rejected rather than ignored, so a
model or client cannot smuggle extra authority-bearing fields through them.
Bump ``CONTRACT_VERSION`` (and the ``schema_version`` literals) on a breaking
change; additive optional fields keep the version.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

CONTRACT_VERSION: Final = 1
SchemaVersion = Literal[1]

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    ),
]

CapabilityName = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Correlation(ContractModel):
    """Identifiers linking user-visible activity to durable records and traces.

    ``operation_id`` is set for anything about one tool execution; it doubles
    as the span-equivalent identifier for diagnostics.
    """

    session_id: Identifier
    run_id: Identifier
    trace_id: Identifier | None = None
    operation_id: Identifier | None = None
