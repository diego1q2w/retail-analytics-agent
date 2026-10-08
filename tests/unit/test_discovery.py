"""Cache, drift, outage and per-identity behavior of schema discovery."""

from __future__ import annotations

from datetime import timedelta

import pytest

from retail_analytics.application.discovery import (
    CatalogUnavailable,
    DiscoveryService,
    RelationNotAvailable,
    SourceSchemaCache,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.catalog import FieldType, SourceType
from tests.unit.discovery_fixtures import CATALOG, FakeClock, StubMetadata, context

pytestmark = pytest.mark.asyncio

HOUR = timedelta(hours=1)


def build(
    stub: StubMetadata | None = None, clock: FakeClock | None = None
) -> tuple[DiscoveryService, SourceSchemaCache, StubMetadata, FakeClock]:
    stub = stub or StubMetadata()
    clock = clock or FakeClock()
    cache = SourceSchemaCache(
        CATALOG, stub, clock, refresh_interval=HOUR, max_stale=timedelta(hours=24)
    )
    return DiscoveryService(CATALOG, cache, clock, stale_after=HOUR), cache, stub, clock


async def test_list_and_describe_share_the_same_fields_and_types() -> None:
    service, *_ = build()
    ctx = context()
    listed = await service.list_relations(ctx)
    assert set(listed.catalog.relations) == {
        "sales_items",
        "products",
        "orders",
        "customers",
    }
    for name, relation in listed.catalog.relations.items():
        _, described = await service.describe_relation(ctx, name)
        assert described == relation
        for field in described.fields:
            assert listed.catalog.field_type(name, field.name) is field.type
    assert listed.catalog.field_type("sales_items", "sale_amount") is FieldType.NUMBER


async def test_warm_cache_reads_source_metadata_once() -> None:
    service, _, stub, clock = build()
    for _ in range(3):
        await service.list_relations(context())
    assert stub.calls == 1
    clock.advance(minutes=61)
    await service.list_relations(context())
    assert stub.calls == 2


async def test_email_like_column_is_never_published() -> None:
    service, cache, stub, clock = build()
    await service.list_relations(context())
    stub.add_column("users", "email_address")
    stub.add_column("order_items", "customer_email")
    clock.advance(minutes=61)
    view = await service.list_relations(context())
    published = {f.name for r in view.catalog.relations.values() for f in r.fields}
    assert not {n for n in published if "mail" in n}
    assert "email_address" in cache.snapshot.health.unreviewed_columns["users"]  # type: ignore[union-attr]


async def test_incompatible_drift_disables_mapping_after_refresh() -> None:
    service, cache, stub, _ = build()
    assert (await service.view_for(context())).catalog.field_type(
        "sales_items", "sale_amount"
    )
    stub.retype("order_items", "sale_price", SourceType.STRING)
    cache.invalidate()
    view = await service.view_for(context())
    assert view.catalog.field_type("sales_items", "sale_amount") is None
    assert view.catalog.field_type("sales_items", "item_status") is FieldType.STRING
    _, items = await service.describe_relation(context(), "sales_items")
    assert items.unavailable_fields == ("sale_amount",)


async def test_authorization_changes_apply_immediately_with_warm_cache() -> None:
    service, _, stub, _ = build()
    a = context(products=frozenset({"1"}), version=1)
    assert (await service.list_relations(a)).catalog.relations
    revoked = context(products=frozenset(), version=2)
    after = await service.list_relations(revoked)
    assert not after.catalog.relations and after.catalog.entitlement_version == 2
    no_permission = context(permissions=frozenset())
    assert not (await service.list_relations(no_permission)).catalog.relations
    with pytest.raises(RelationNotAvailable):
        await service.describe_relation(revoked, "sales_items")
    assert stub.calls == 1


async def test_two_identities_do_not_share_filtered_views() -> None:
    service, *_ = build()
    a = context(executive="exec-a", version=1)
    b = context(
        executive="exec-b",
        products=frozenset(),
        version=7,
        permissions=frozenset({Permission.REPORTS_READ_OWN.value}),
    )
    va = await service.list_relations(a)
    vb = await service.list_relations(b)
    assert va.catalog.relations and not vb.catalog.relations
    assert (va.catalog.entitlement_version, vb.catalog.entitlement_version) == (1, 7)
    assert (await service.list_relations(a)).catalog.relations


async def test_outage_serves_stale_snapshot_then_fails_closed() -> None:
    service, _, stub, clock = build()
    await service.list_relations(context())
    stub.fail = True
    clock.advance(minutes=61)
    view = await service.list_relations(context())
    assert view.stale and view.catalog.relations
    clock.advance(hours=25)
    with pytest.raises(CatalogUnavailable):
        await service.list_relations(context())


async def test_outage_without_snapshot_fails_closed_and_backs_off() -> None:
    service, _, stub, clock = build()
    stub.fail = True
    for _ in range(3):
        with pytest.raises(CatalogUnavailable):
            await service.list_relations(context())
    assert stub.calls == 1
    clock.advance(seconds=31)
    stub.fail = False
    assert (await service.list_relations(context())).catalog.relations
    assert stub.calls == 2


async def test_unknown_relation_error_reveals_nothing_about_the_source() -> None:
    service, *_ = build()
    with pytest.raises(RelationNotAvailable) as raised:
        await service.describe_relation(context(), "order_items")
    assert "users" not in str(raised.value) and "email" not in str(raised.value)


async def test_refresh_interval_must_be_positive_and_not_exceed_max_stale() -> None:
    with pytest.raises(ValueError):
        SourceSchemaCache(
            CATALOG,
            StubMetadata(),
            FakeClock(),
            refresh_interval=timedelta(hours=2),
            max_stale=HOUR,
        )
