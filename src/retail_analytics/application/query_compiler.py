"""Port and result types of the restricted analytical SQL compiler.

The model never submits SQL for execution. It submits analytical SQL over the
logical relations of a :class:`CatalogView`; a ``QueryCompiler`` checks it
against an explicit grammar and the view, then binds every logical relation to
a trusted, product-scoped projection. Only the compiled result reaches the
warehouse.

Everything here is SDK-free: the compiled statement is BigQuery SQL text plus
typed parameters, ready for an executor adapter. Authority (catalog view and
product scope) is passed separately from the model-authored query and is never
read from it.
"""

from __future__ import annotations

from retail_analytics.domain.operations import ToolErrorCode

# Operational default (1 GiB per query); the executor must apply it to the job.
DEFAULT_MAXIMUM_BYTES_BILLED = 1024**3

# Parameter names with these prefixes belong to the compiler; analysis values
# and SQL identifiers may not use them.
RESERVED_PREFIXES = ("_policy_", "_value_")


class QueryRejected(Exception):
    """The query cannot be compiled. Safe to show to the model.

    ``correctable`` tells the agent whether reformulating within the supported
    subset can succeed. Unknown and forbidden names are deliberately reported
    alike so diagnostics never reveal unpublished schema.
    """

    def __init__(
        self,
        code: ToolErrorCode,
        reason: str,
        message: str,
        *,
        relation: str | None = None,
        field: str | None = None,
        available_fields: tuple[str, ...] = (),
        cause_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message
        self.relation = relation
        self.field = field
        self.available_fields = available_fields
        # Class name of the parser/optimizer exception that caused a fail-closed
        # rejection (never its message): counted by telemetry.
        self.cause_type = cause_type

    @property
    def correctable(self) -> bool:
        return self.code in (
            ToolErrorCode.INVALID_QUERY,
            ToolErrorCode.UNSUPPORTED_SQL,
            ToolErrorCode.INVALID_INPUT,
            ToolErrorCode.FIELD_UNAVAILABLE,
        )

    def __repr__(self) -> str:
        return f"QueryRejected({self.code.value}, {self.reason!r})"
