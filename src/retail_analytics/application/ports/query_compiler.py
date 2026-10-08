from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.catalog import CatalogView


class QueryCompiler(Protocol):
    """Compiles model SQL against one executive's current authority.

    ``catalog`` and ``scope`` must be resolved freshly for the attempt by
    trusted code. Raises :class:`QueryRejected`; never returns a statement that
    reads an unbound or unscoped source.
    """

    def compile(
        self, query: AnalysisQuery, *, catalog: CatalogView, scope: ProductScope
    ) -> CompiledQuery: ...


class ScopedQueryCompilers(Protocol):
    """Hands out the compiler for one executive's reference scope.

    Opaque references are keyed per executive, so the compiler is selected
    with the trusted executive identity (never a model argument) for every
    attempt; references from another executive's results match nothing.
    """

    def for_executive(self, executive_id: str) -> QueryCompiler: ...
