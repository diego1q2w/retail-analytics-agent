"""Constructors for the compiler's model-safe rejections."""

from __future__ import annotations

from retail_analytics.application.query_compiler import QueryRejected
from retail_analytics.domain.operations import ToolErrorCode

# Echoed model-supplied names are truncated; they are the model's own text and
# are reported identically whether the name is unknown or forbidden.
_MAX_ECHO = 64


def reject(
    code: ToolErrorCode,
    reason: str,
    message: str,
    *,
    relation: str | None = None,
    field: str | None = None,
    available_fields: tuple[str, ...] = (),
) -> QueryRejected:
    return QueryRejected(
        code,
        reason,
        message,
        relation=relation[:_MAX_ECHO] if relation else None,
        field=field[:_MAX_ECHO] if field else None,
        available_fields=available_fields,
    )


def unsupported(reason: str, message: str) -> QueryRejected:
    return reject(ToolErrorCode.UNSUPPORTED_SQL, reason, message)


def field_unavailable(relation: str | None, field: str | None) -> QueryRejected:
    where = f" on {relation}" if relation else ""
    return reject(
        ToolErrorCode.FIELD_UNAVAILABLE,
        "field_unavailable",
        f"The field is not available{where}; use describe_relation for the "
        "fields you may use",
        relation=relation,
        field=field,
    )
