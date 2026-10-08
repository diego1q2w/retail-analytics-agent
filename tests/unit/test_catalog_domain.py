"""Catalog invariants, privacy exclusions and drift evaluation (pure domain)."""

from __future__ import annotations

import pytest

from retail_analytics.domain.catalog import (
    CatalogError,
    Derivation,
    DriftKind,
    FieldDefinition,
    FieldType,
    SourceColumnRef,
    SourceSchema,
    SourceType,
    build_view,
    evaluate_health,
)
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.logical_fields import LOGICAL_FIELDS
from tests.unit.discovery_fixtures import StubMetadata, thelook_schema

CATALOG = default_logical_catalog()


def _schema(stub: StubMetadata | None = None) -> SourceSchema:
    tables = (stub.tables if stub else thelook_schema()).items()
    return SourceSchema({t: tuple(c) for t, c in tables})


def test_catalog_matches_the_logical_fields_metrics_validate_against() -> None:
    for relation in CATALOG.relations:
        expected = next(
            f for r, f in LOGICAL_FIELDS.items() if r.value == relation.name
        )
        assert {f.name for f in relation.fields} == set(expected)


def test_reference_schema_is_healthy_and_unreviewed_columns_are_unpublished() -> None:
    health = evaluate_health(CATALOG, _schema())
    assert not health.issues and not health.disabled_relations
    users = health.unreviewed_columns["users"]
    assert {"email", "first_name", "latitude"} <= set(users)
    view = build_view(CATALOG, health, entitlement_version=1, visible=True)
    published = {f.name for r in view.relations.values() for f in r.fields}
    assert not published & {"email", "first_name", "age", "street_address", "id"}


@pytest.mark.parametrize(
    "column", ["email", "first_name", "street_address", "latitude"]
)
def test_direct_identifiers_can_never_back_a_field(column: str) -> None:
    with pytest.raises(CatalogError):
        FieldDefinition(
            "x",
            FieldType.STRING,
            "bad",
            Derivation.DIRECT,
            (SourceColumnRef("users", column, frozenset({SourceType.STRING})),),
        )


def test_exact_age_and_raw_keys_need_their_safe_derivation() -> None:
    age = SourceColumnRef("users", "age", frozenset({SourceType.INT64}))
    with pytest.raises(CatalogError):
        FieldDefinition("age", FieldType.INTEGER, "exact", Derivation.DIRECT, (age,))
    key = SourceColumnRef("users", "id", frozenset({SourceType.INT64}))
    with pytest.raises(CatalogError):
        FieldDefinition("uid", FieldType.INTEGER, "raw", Derivation.DIRECT, (key,))
    FieldDefinition("band", FieldType.STRING, "ok", Derivation.AGE_BAND, (age,))


def test_no_field_exposes_raw_age_or_identifier_columns() -> None:
    for relation in CATALOG.relations:
        for field in relation.fields:
            for source in field.sources:
                if source.column == "age":
                    assert field.derivation is Derivation.AGE_BAND
                if source.column in {"user_id", "order_id"} or (
                    source.column == "id" and source.table != "products"
                ):
                    assert field.derivation in {
                        Derivation.OPAQUE_REFERENCE,
                        Derivation.PERMITTED_ITEM_COUNT,
                    }


def test_missing_column_disables_only_the_affected_field() -> None:
    stub = StubMetadata()
    stub.drop_column("order_items", "sale_price")
    health = evaluate_health(CATALOG, _schema(stub))
    assert health.disabled_fields == {("sales_items", "sale_amount")}
    assert not health.disabled_relations
    assert health.issues[0].kind is DriftKind.MISSING_COLUMN
    view = build_view(CATALOG, health, entitlement_version=1, visible=True)
    items = view.relations["sales_items"]
    assert items.field("sale_amount") is None and items.field("item_status")
    assert items.unavailable_fields == ("sale_amount",)


def test_incompatible_type_on_essential_field_disables_relation_and_joins() -> None:
    stub = StubMetadata()
    stub.retype("products", "id", SourceType.STRING)
    health = evaluate_health(CATALOG, _schema(stub))
    assert health.issues[0].kind is DriftKind.INCOMPATIBLE_TYPE
    assert "products" in health.disabled_relations
    view = build_view(CATALOG, health, entitlement_version=1, visible=True)
    assert "products" not in view.relations
    assert all(j.target != "products" for j in view.relations["sales_items"].joins)
    assert view.relations["sales_items"].field("product_id") is not None


def test_missing_table_disables_dependent_fields() -> None:
    stub = StubMetadata()
    del stub.tables["users"]
    health = evaluate_health(CATALOG, _schema(stub))
    assert {i.kind for i in health.issues} == {DriftKind.MISSING_TABLE}
    assert "customers" in health.disabled_relations


def test_invisible_executive_gets_an_empty_view_not_everything() -> None:
    health = evaluate_health(CATALOG, _schema())
    view = build_view(CATALOG, health, entitlement_version=4, visible=False)
    assert not view.relations and view.entitlement_version == 4


def test_join_field_types_must_agree() -> None:
    assert CATALOG.relation("sales_items") is not None
    for relation in CATALOG.relations:
        for join in relation.joins:
            target = CATALOG.relation(join.target)
            assert target is not None
            local = relation.field(join.field)
            remote = target.field(join.target_field)
            assert local and remote and local.type is remote.type
