from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.domain.catalog import CatalogView


@dataclass(frozen=True, slots=True)
class QueryAuthority:
    """Authority resolved for one attempt: never cached across attempts."""

    context: ExecutionContext
    catalog: CatalogView
