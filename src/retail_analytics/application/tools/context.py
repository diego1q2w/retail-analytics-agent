"""Trusted execution context: what the backend knows and the model never sets.

These are plain dataclasses, not contract models: they are never part of a
model-facing schema and are loaded fresh by application code for every
execution (inside retryable activities), not deserialized from model output.
Run budgets will join ``ExecutionContext`` when budget enforcement lands; they
must not become tool arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from retail_analytics.application.contracts import Correlation
from retail_analytics.domain.access import ProductScope


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Authenticated executive, current authority and correlation for one run."""

    executive_id: str
    permissions: frozenset[str]
    product_scope: ProductScope
    correlation: Correlation

    def __post_init__(self) -> None:
        if not self.executive_id:
            raise ValueError("executive_id is required")
        if self.correlation.operation_id is not None:
            raise ValueError("run context must not carry an operation_id")


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Context for one attempt of one tool execution.

    ``operation_id`` is application-generated, stable across retries and
    resumption of the same operation, and is the idempotency key handlers use
    for external effects.
    """

    execution: ExecutionContext
    operation_id: str
    attempt: int = 1

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("attempt starts at 1")
        self.correlation  # noqa: B018 - validates operation_id as an Identifier

    @property
    def correlation(self) -> Correlation:
        return Correlation.model_validate(
            {
                **self.execution.correlation.model_dump(),
                "operation_id": self.operation_id,
            }
        )

    def next_attempt(self) -> OperationContext:
        return replace(self, attempt=self.attempt + 1)
