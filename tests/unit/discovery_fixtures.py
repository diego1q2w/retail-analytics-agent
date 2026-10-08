"""Stub source metadata and trusted contexts for discovery tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from retail_analytics.application.contracts import Correlation
from retail_analytics.application.discovery import SourceMetadataUnavailable
from retail_analytics.application.tools import ExecutionContext
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.catalog import SourceColumn, SourceSchema, SourceType
from retail_analytics.domain.logical_catalog import default_logical_catalog

_S, _I, _F, _T = (
    SourceType.STRING,
    SourceType.INT64,
    SourceType.FLOAT64,
    SourceType.TIMESTAMP,
)


def thelook_schema() -> dict[str, list[SourceColumn]]:
    """The provisional source layout, including columns the catalog never maps."""

    def cols(**types: SourceType) -> list[SourceColumn]:
        return [SourceColumn(name, kind) for name, kind in types.items()]

    return {
        "order_items": cols(
            id=_I,
            order_id=_I,
            user_id=_I,
            product_id=_I,
            status=_S,
            sale_price=_F,
            inventory_item_id=_I,
            created_at=_T,
        ),
        "orders": cols(
            order_id=_I, user_id=_I, status=_S, created_at=_T, num_of_item=_I
        ),
        "products": cols(
            id=_I,
            name=_S,
            category=_S,
            brand=_S,
            department=_S,
            retail_price=_F,
            cost=_F,
            sku=_S,
        ),
        "users": cols(
            id=_I,
            first_name=_S,
            last_name=_S,
            email=_S,
            age=_I,
            gender=_S,
            state=_S,
            street_address=_S,
            postal_code=_S,
            city=_S,
            country=_S,
            latitude=_F,
            longitude=_F,
        ),
    }


class StubMetadata:
    """A SourceMetadataProvider whose schema and availability tests control."""

    def __init__(self, tables: dict[str, list[SourceColumn]] | None = None) -> None:
        self.tables = tables if tables is not None else thelook_schema()
        self.calls = 0
        self.fail = False

    async def read_schema(self, tables: frozenset[str]) -> SourceSchema:
        self.calls += 1
        if self.fail:
            raise SourceMetadataUnavailable("down")
        return SourceSchema(
            {t: tuple(c) for t, c in self.tables.items() if t in tables}
        )

    def add_column(self, table: str, name: str, kind: SourceType = _S) -> None:
        self.tables[table].append(SourceColumn(name, kind))

    def drop_column(self, table: str, name: str) -> None:
        self.tables[table] = [c for c in self.tables[table] if c.name != name]

    def retype(self, table: str, name: str, kind: SourceType) -> None:
        self.tables[table] = [
            SourceColumn(c.name, kind) if c.name == name else c
            for c in self.tables[table]
        ]


@dataclass
class FakeClock:
    now: datetime = field(default_factory=lambda: datetime(2026, 10, 8, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def context(
    *,
    products: frozenset[str] = frozenset({"1", "2"}),
    version: int = 1,
    permissions: frozenset[str] = frozenset({Permission.ANALYSIS_READ.value}),
    executive: str = "exec-a",
) -> ExecutionContext:
    return ExecutionContext(
        executive_id=executive,
        permissions=permissions,
        product_scope=ProductScope(products, version),
        correlation=Correlation(session_id="s-1", run_id="r-1"),
    )


CATALOG = default_logical_catalog()
