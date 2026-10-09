"""Versioned logical catalog: the reviewed analytical surface and its source mappings.

The catalog is an allowlist. A logical field exists only because a reviewer
mapped it to specific source columns; a source column that is not mapped is
unreachable no matter what the warehouse metadata says. Discovery tools and the
SQL compiler consume the same :class:`CatalogView`, so what the model is told
and what validation accepts cannot diverge.

Source drift is evaluated by :func:`evaluate_health`: a mapping whose columns
are missing or no longer type-compatible disables that field (and the relation,
when the field is essential) instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class FieldType(StrEnum):
    """Type of a logical field as the model and the compiler see it."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    DATE = "date"
    # Opaque application reference (never a raw source identifier).
    REFERENCE = "reference"


class SourceType(StrEnum):
    """Normalized warehouse column type; adapters map SDK types onto these."""

    STRING = "string"
    INT64 = "int64"
    FLOAT64 = "float64"
    NUMERIC = "numeric"
    BOOL = "bool"
    DATE = "date"
    TIMESTAMP = "timestamp"
    # Anything the catalog does not understand (arrays, structs, geography...).
    OTHER = "other"


class Derivation(StrEnum):
    """How trusted code builds a logical field from its source columns."""

    DIRECT = "direct"
    OPAQUE_REFERENCE = "opaque_reference"
    AGE_BAND = "age_band"
    DATE_OF_TIMESTAMP = "date_of_timestamp"
    PERMITTED_ITEM_COUNT = "permitted_item_count"


class Cardinality(StrEnum):
    MANY_TO_ONE = "many_to_one"
    ONE_TO_ONE = "one_to_one"


# Source columns that may never back a logical field directly. Raw keys can only
# become opaque references; exact age only an age band. Names, contact details
# and fine location have no permitted derivation at all.
IDENTIFIER_COLUMNS: frozenset[str] = frozenset({"id", "order_id", "user_id"})
EXACT_AGE_COLUMNS: frozenset[str] = frozenset({"age"})
DIRECT_IDENTIFIER_COLUMNS: frozenset[str] = frozenset(
    {
        "first_name",
        "last_name",
        "name_of_customer",
        "email",
        "street_address",
        "postal_code",
        "city",
        "latitude",
        "longitude",
        "user_geom",
    }
)
# Tables whose ``id`` is a product key, which is an ordinary reviewed field.
PRODUCT_KEY_TABLE = "products"

_REFERENCE_DERIVATIONS = frozenset({Derivation.OPAQUE_REFERENCE})
_AGE_DERIVATIONS = frozenset({Derivation.AGE_BAND})
_COUNT_DERIVATIONS = frozenset({Derivation.PERMITTED_ITEM_COUNT})

_COMPATIBLE_SOURCE_TYPES: Mapping[FieldType, frozenset[SourceType]] = MappingProxyType(
    {
        FieldType.STRING: frozenset({SourceType.STRING}),
        FieldType.INTEGER: frozenset({SourceType.INT64}),
        FieldType.NUMBER: frozenset({SourceType.FLOAT64, SourceType.NUMERIC}),
        FieldType.DATE: frozenset({SourceType.DATE, SourceType.TIMESTAMP}),
        FieldType.REFERENCE: frozenset(
            {SourceType.INT64, SourceType.STRING, SourceType.NUMERIC}
        ),
    }
)


class CatalogError(ValueError):
    """The catalog definition itself is invalid (a programming/review error)."""


@dataclass(frozen=True, slots=True)
class SourceColumnRef:
    """One source column a logical field depends on.

    ``accepted`` is the set of warehouse types the field's derivation can
    handle; a different type is treated as incompatible drift.
    """

    table: str
    column: str
    accepted: frozenset[SourceType]

    def __post_init__(self) -> None:
        if not (self.table and self.column and self.accepted):
            raise CatalogError("source column needs table, column and accepted types")


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    name: str
    type: FieldType
    description: str
    derivation: Derivation
    sources: tuple[SourceColumnRef, ...]
    # Without an essential field the relation cannot be used at all.
    essential: bool = False
    # A customer demographic (for example state or age band): usable only in
    # group-level statistics, never next to or about one customer, order or item.
    demographic: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.description.strip() or not self.sources:
            raise CatalogError(f"field {self.name!r} is incomplete")
        for source in self.sources:
            self._check_source(source)
        if self.derivation is Derivation.DIRECT and not all(
            s.accepted <= _COMPATIBLE_SOURCE_TYPES[self.type] for s in self.sources
        ):
            raise CatalogError(f"{self.name}: source types do not fit {self.type}")
        if self.demographic and self.derivation is Derivation.OPAQUE_REFERENCE:
            raise CatalogError(f"{self.name}: a reference cannot be a demographic")

    def _check_source(self, source: SourceColumnRef) -> None:
        column = source.column
        where = f"{self.name} <- {source.table}.{column}"
        if column in DIRECT_IDENTIFIER_COLUMNS:
            raise CatalogError(f"direct identifier cannot back a field: {where}")
        if column in EXACT_AGE_COLUMNS and self.derivation not in _AGE_DERIVATIONS:
            raise CatalogError(f"exact age needs an age band derivation: {where}")
        if (
            column in IDENTIFIER_COLUMNS
            and source.table != PRODUCT_KEY_TABLE
            and self.derivation not in (_REFERENCE_DERIVATIONS | _COUNT_DERIVATIONS)
        ):
            raise CatalogError(f"raw key needs an opaque reference: {where}")


@dataclass(frozen=True, slots=True)
class JoinDefinition:
    """A reviewed join: equality of ``field`` with ``target_field`` on the target."""

    field: str
    target: str
    target_field: str
    cardinality: Cardinality


@dataclass(frozen=True, slots=True)
class RelationDefinition:
    name: str
    description: str
    grain: str
    fields: tuple[FieldDefinition, ...]
    joins: tuple[JoinDefinition, ...] = ()

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names) or not names:
            raise CatalogError(f"{self.name}: field names must be unique and non-empty")
        for join in self.joins:
            if join.field not in names:
                raise CatalogError(f"{self.name}: join on unknown field {join.field}")

    def field(self, name: str) -> FieldDefinition | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True, slots=True)
class LogicalCatalog:
    version: int
    relations: tuple[RelationDefinition, ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise CatalogError("catalog version must be >= 1")
        by_name = {r.name: r for r in self.relations}
        if len(by_name) != len(self.relations):
            raise CatalogError("relation names must be unique")
        for relation in self.relations:
            for join in relation.joins:
                target = by_name.get(join.target)
                if target is None or target.field(join.target_field) is None:
                    raise CatalogError(f"{relation.name}: join to unknown target")
                local = relation.field(join.field)
                remote = target.field(join.target_field)
                if local is None or remote is None or local.type is not remote.type:
                    raise CatalogError(f"{relation.name}: join field types differ")

    def relation(self, name: str) -> RelationDefinition | None:
        return next((r for r in self.relations if r.name == name), None)

    @property
    def source_tables(self) -> frozenset[str]:
        return frozenset(
            s.table for r in self.relations for f in r.fields for s in f.sources
        )


# --- source metadata and drift ------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceColumn:
    name: str
    type: SourceType


@dataclass(frozen=True, slots=True)
class SourceSchema:
    """Columns of every source table, as last read from the warehouse."""

    tables: Mapping[str, tuple[SourceColumn, ...]]


class DriftKind(StrEnum):
    MISSING_TABLE = "missing_table"
    MISSING_COLUMN = "missing_column"
    INCOMPATIBLE_TYPE = "incompatible_type"


@dataclass(frozen=True, slots=True)
class DriftIssue:
    """Operator-facing detail. Never returned to the model or an executive."""

    kind: DriftKind
    relation: str
    field: str
    table: str
    column: str | None


@dataclass(frozen=True, slots=True)
class CatalogHealth:
    catalog_version: int
    issues: tuple[DriftIssue, ...]
    disabled_fields: frozenset[tuple[str, str]]
    disabled_relations: frozenset[str]
    # Source columns present in the warehouse that no reviewed field maps. They
    # stay unpublished; only the count is meant for non-operator surfaces.
    unreviewed_columns: Mapping[str, tuple[str, ...]]

    @property
    def unreviewed_column_count(self) -> int:
        return sum(len(cols) for cols in self.unreviewed_columns.values())


def evaluate_health(catalog: LogicalCatalog, schema: SourceSchema) -> CatalogHealth:
    issues: list[DriftIssue] = []
    disabled_fields: set[tuple[str, str]] = set()
    disabled_relations: set[str] = set()
    for relation in catalog.relations:
        for field in relation.fields:
            problems = _field_issues(relation.name, field, schema)
            if problems:
                issues.extend(problems)
                disabled_fields.add((relation.name, field.name))
                if field.essential:
                    disabled_relations.add(relation.name)
    mapped: dict[str, set[str]] = {}
    for relation in catalog.relations:
        for field in relation.fields:
            for source in field.sources:
                mapped.setdefault(source.table, set()).add(source.column)
    unreviewed = {
        table: tuple(sorted(c.name for c in columns if c.name not in mapped[table]))
        for table, columns in schema.tables.items()
        if table in mapped
    }
    return CatalogHealth(
        catalog_version=catalog.version,
        issues=tuple(issues),
        disabled_fields=frozenset(disabled_fields),
        disabled_relations=frozenset(disabled_relations),
        unreviewed_columns=MappingProxyType(
            {t: cols for t, cols in unreviewed.items() if cols}
        ),
    )


def _field_issues(
    relation: str, field: FieldDefinition, schema: SourceSchema
) -> list[DriftIssue]:
    found: list[DriftIssue] = []
    for source in field.sources:
        columns = schema.tables.get(source.table)
        if columns is None:
            found.append(
                DriftIssue(
                    DriftKind.MISSING_TABLE, relation, field.name, source.table, None
                )
            )
            continue
        actual = next((c for c in columns if c.name == source.column), None)
        if actual is None:
            kind = DriftKind.MISSING_COLUMN
        elif actual.type not in source.accepted:
            kind = DriftKind.INCOMPATIBLE_TYPE
        else:
            continue
        found.append(
            DriftIssue(kind, relation, field.name, source.table, source.column)
        )
    return found


def compatible_source_types(field_type: FieldType) -> frozenset[SourceType]:
    return _COMPATIBLE_SOURCE_TYPES[field_type]


# --- the view shared by discovery and validation ------------------------------


@dataclass(frozen=True, slots=True)
class FieldView:
    name: str
    type: FieldType
    description: str
    derivation: Derivation
    sources: tuple[SourceColumnRef, ...]
    demographic: bool = False

    @property
    def is_identity(self) -> bool:
        """An opaque reference to one customer, order or item."""
        return self.derivation is Derivation.OPAQUE_REFERENCE


@dataclass(frozen=True, slots=True)
class JoinView:
    field: str
    target: str
    target_field: str
    cardinality: Cardinality


@dataclass(frozen=True, slots=True)
class RelationView:
    name: str
    description: str
    grain: str
    fields: tuple[FieldView, ...]
    joins: tuple[JoinView, ...]
    # Logical names of reviewed fields that drift currently disables.
    unavailable_fields: tuple[str, ...] = ()

    def field(self, name: str) -> FieldView | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True, slots=True)
class CatalogView:
    """What one executive may use right now: the contract for T08 and discovery.

    Only reviewed, currently compatible fields appear. An empty view means no
    data is accessible, never unrestricted access.
    """

    catalog_version: int
    entitlement_version: int
    relations: Mapping[str, RelationView]

    def relation(self, name: str) -> RelationView | None:
        return self.relations.get(name)

    def field_type(self, relation: str, field: str) -> FieldType | None:
        rel = self.relations.get(relation)
        found = rel.field(field) if rel else None
        return found.type if found else None


def build_view(
    catalog: LogicalCatalog,
    health: CatalogHealth,
    *,
    entitlement_version: int,
    visible: bool,
) -> CatalogView:
    """Project the catalog through drift health for one authorized executive.

    ``visible`` is the application's verdict (permission + non-empty product
    scope); when False the view is empty.
    """
    relations: dict[str, RelationView] = {}
    if visible:
        for relation in catalog.relations:
            if relation.name in health.disabled_relations:
                continue
            relations[relation.name] = _relation_view(relation, health)
    return CatalogView(
        catalog.version, entitlement_version, MappingProxyType(relations)
    )


def _relation_view(relation: RelationDefinition, health: CatalogHealth) -> RelationView:
    def enabled(rel: str, field: str) -> bool:
        return (
            rel not in health.disabled_relations
            and (rel, field) not in health.disabled_fields
        )

    fields = tuple(
        FieldView(
            f.name,
            f.type,
            f.description,
            f.derivation,
            f.sources,
            demographic=f.demographic,
        )
        for f in relation.fields
        if enabled(relation.name, f.name)
    )
    joins = tuple(
        JoinView(j.field, j.target, j.target_field, j.cardinality)
        for j in relation.joins
        if enabled(relation.name, j.field) and enabled(j.target, j.target_field)
    )
    unavailable = tuple(
        f.name for f in relation.fields if not enabled(relation.name, f.name)
    )
    return RelationView(
        relation.name, relation.description, relation.grain, fields, joins, unavailable
    )
