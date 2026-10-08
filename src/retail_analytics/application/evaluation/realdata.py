"""Reproducible real-data benchmark: spec, frozen extract, expected values, drift.

The public source tables change daily, and a closed date window does not freeze
them (statuses restate, rows are reloaded). So the benchmark is scored against a
versioned, sanitized *frozen extract* of the source tables, never against the
live warehouse:

1. ``extract`` copies the rows needed for the fixed window out of BigQuery with
   identifiers replaced by dense pseudonyms and ages reduced to 5-year bands
   (no names, e-mails, addresses or raw identifiers leave the warehouse). Its
   manifest records source tables, query fingerprints, bytes, time and digest.
2. Expected values come from checked reference SQL, run twice by two
   structurally different routes over the extract. They are stored together with
   the extract digest, so the numbers are tied to one data version.
3. The conversation manifest is generated from the spec and the expected values
   (the numbers are never typed by hand).
4. A *drift report* re-runs the same reference SQL on the live warehouse and
   compares it with the expected values. It is a separate artifact about the
   source, not a benchmark verdict, and its outcome never changes a score.

This module is pure: SQL execution and file access are ports/adapters.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import Field, model_validator

from retail_analytics.application.contracts import ContractModel, Identifier
from retail_analytics.application.evaluation.manifest import (
    ExactExpectation,
    Expectation,
    JudgeSpec,
    Manifest,
    NumericExpectation,
    Scalar,
    Scenario,
    ScopeSpec,
    TextExpectation,
    Turn,
)

SPEC_SCHEMA_VERSION: Final = 1
MANIFEST_ID: Final = "realdata-reference-conversations"
ID_PREFIX: Final = "rd-"
# Capabilities every real-data scenario needs. ``frozen_extract_source`` means a
# target that answers from the frozen extract (not the live warehouse).
REQUIRES: Final = ("agent_runtime", "frozen_extract_source")

# The only columns the frozen extract may contain (the sanitization contract).
# ids are dense pseudonyms 1..N; ``users.age`` is the start of a 5-year band.
EXTRACT_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "orders": ("order_id", "user_id", "created_at"),
    "order_items": ("id", "order_id", "user_id", "product_id", "status", "sale_price"),
    "users": ("id", "age", "state"),
    "products": ("id", "name", "category"),
}
FORBIDDEN_TOKENS: Final = (
    "first_name",
    "last_name",
    "email",
    "street_address",
    "postal_code",
    "latitude",
    "longitude",
    "user_geom",
    "traffic_source",
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|merge|truncate|grant|export|call)\b",
    re.IGNORECASE,
)

OutputKind = Literal["money", "count", "share", "text"]


# ------------------------------------------------------------------ spec


class OutputSpec(ContractModel):
    kind: OutputKind
    tol: Annotated[float, Field(ge=0)] = 0.0


class ScopeDef(ContractModel):
    executive_ref: Identifier
    product_lo: Annotated[int, Field(ge=1)]
    product_hi: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _ordered(self) -> ScopeDef:
        if self.product_lo > self.product_hi:
            raise ValueError("product_lo must not exceed product_hi")
        return self

    @property
    def product_scope(self) -> str:
        return f"products:{self.product_lo}-{self.product_hi}"


class ExtractSpec(ContractModel):
    source_dataset: Annotated[str, Field(pattern=r"^[a-z0-9-]+\.[a-z0-9_]+$")]
    window_start: str
    window_end_exclusive: str
    date_basis: str
    # Windows must end at least this many days before the extraction day.
    min_settled_days: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _window(self) -> ExtractSpec:
        start, end = _iso(self.window_start), _iso(self.window_end_exclusive)
        if start >= end:
            raise ValueError("extract window must be non-empty")
        return self


class QuerySpec(ContractModel):
    scope: Identifier
    params: Mapping[Identifier, str]
    outputs: Mapping[Identifier, OutputSpec] = Field(min_length=1)
    sql: Identifier
    # Filled in by the file loader from reference-sql/<sql>.{primary,crosscheck}.sql.
    primary_sql: str = ""
    crosscheck_sql: str = ""


class ExpectationSpec(ContractModel):
    kind: Literal["numeric", "exact", "contains", "not_contains"]
    name: Identifier
    # "<query_id>.<output column>" for data-derived values; ``value`` for flags.
    source: str | None = None
    value: Scalar = None
    abs_tol: Annotated[float, Field(ge=0)] = 0.0

    @model_validator(mode="after")
    def _one_origin(self) -> ExpectationSpec:
        if (self.source is None) == (self.value is None):
            raise ValueError("give exactly one of source or value")
        if self.kind in {"numeric", "contains", "not_contains"} and self.source is None:
            raise ValueError(f"{self.kind} expectations need a source")
        return self


class ScenarioSpec(ContractModel):
    id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=200)]
    level: Literal[1, 2, 3]
    category: Identifier
    scope: Identifier
    dialogue: tuple[Annotated[str, Field(min_length=1, max_length=8000)], ...] = Field(
        min_length=1
    )
    tags: tuple[Identifier, ...] = ()
    expectations: tuple[ExpectationSpec, ...] = Field(min_length=1)
    judge: JudgeSpec | None = None


class BenchmarkSpec(ContractModel):
    schema_version: Literal[1] = SPEC_SCHEMA_VERSION
    benchmark_id: Identifier
    benchmark_version: Identifier
    data_ref: Identifier
    extract: ExtractSpec
    scopes: Mapping[Identifier, ScopeDef] = Field(min_length=1)
    queries: Mapping[Identifier, QuerySpec] = Field(min_length=1)
    scenarios: tuple[ScenarioSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> BenchmarkSpec:
        start = _iso(self.extract.window_start)
        end = _iso(self.extract.window_end_exclusive)
        for qid, query in self.queries.items():
            if query.scope not in self.scopes:
                raise ValueError(f"{qid}: unknown scope {query.scope}")
            for name, value in query.params.items():
                if _DATE.match(value):
                    if not start <= _iso(value) <= end:
                        raise ValueError(
                            f"{qid}.{name} lies outside the extract window"
                        )
                else:
                    raise ValueError(f"{qid}.{name}: only ISO dates are accepted")
        ids = [s.id for s in self.scenarios]
        if len(set(ids)) != len(ids) or not all(i.startswith(ID_PREFIX) for i in ids):
            raise ValueError(f"scenario ids must be unique and start with {ID_PREFIX}")
        for scenario in self.scenarios:
            if scenario.scope not in self.scopes:
                raise ValueError(f"{scenario.id}: unknown scope {scenario.scope}")
            for exp in scenario.expectations:
                if exp.source is None:
                    continue
                qid, _, column = exp.source.partition(".")
                source = self.queries.get(qid)
                if source is None or column not in source.outputs:
                    raise ValueError(f"{scenario.id}: unknown source {exp.source}")
                if source.scope != scenario.scope:
                    raise ValueError(f"{scenario.id}: {qid} uses another scope")
        return self


def _iso(text: str) -> date:
    if not _DATE.match(text):
        raise ValueError(f"not an ISO date: {text!r}")
    return date.fromisoformat(text)


def canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_digest(spec: BenchmarkSpec) -> str:
    return sha256_hex(canonical_json(spec.model_dump(mode="json")))


# ------------------------------------------------------------- rendering


def render_sql(
    template: str, spec: BenchmarkSpec, query_id: str, *, dataset_ref: str
) -> str:
    """Bind a reference statement's placeholders to validated values.

    Only ISO dates and integer product bounds are substituted, so no free text
    can enter a statement. Statements must be read-only."""
    query = spec.queries[query_id]
    scope = spec.scopes[query.scope]
    values: dict[str, str] = {
        "ds": dataset_ref,
        "scope_lo": str(scope.product_lo),
        "scope_hi": str(scope.product_hi),
    }
    for name, value in query.params.items():
        values[name] = value
        values[f"{name}_key"] = value[:4] + value[5:7]

    def bind(match: re.Match[str]) -> str:
        try:
            return values[match.group(1)]
        except KeyError:
            raise ValueError(
                f"{query_id}: unbound placeholder {match.group(0)}"
            ) from None

    sql = _PLACEHOLDER.sub(bind, template)
    if _SQL_FORBIDDEN.search(sql):
        raise ValueError(f"{query_id}: reference SQL must be read-only")
    return sql


def sql_fingerprint(sql: str) -> str:
    return sha256_hex(" ".join(sql.split()))[:32]


# ----------------------------------------------------------- provenance


class TableFile(ContractModel):
    table: Identifier
    path: str
    rows: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ExtractQueryRecord(ContractModel):
    table: Identifier
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    job_id: str
    bytes_processed: Annotated[int, Field(ge=0)]
    bytes_billed: Annotated[int, Field(ge=0)]


class SourceTableInfo(ContractModel):
    num_rows: int | None
    modified: str | None


class ExtractManifest(ContractModel):
    schema_version: Literal[1] = 1
    data_ref: Identifier
    extract_version: Identifier
    extracted_at: str
    extractor: str
    source_dataset: str
    location: str
    window_start: str
    window_end_exclusive: str
    date_basis: str
    sanitization: tuple[str, ...]
    source_tables: Mapping[Identifier, SourceTableInfo]
    files: tuple[TableFile, ...]
    queries: tuple[ExtractQueryRecord, ...]
    extract_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def compute_extract_digest(manifest: ExtractManifest) -> str:
    """Identity of the extract: its files, how they were produced and its window."""
    return sha256_hex(
        canonical_json(
            {
                "data_ref": manifest.data_ref,
                "extract_version": manifest.extract_version,
                "window": [manifest.window_start, manifest.window_end_exclusive],
                "files": sorted((f.table, f.sha256, f.rows) for f in manifest.files),
                "queries": sorted((q.table, q.fingerprint) for q in manifest.queries),
            }
        )
    )


# ---------------------------------------------------------- execution


@dataclass(frozen=True)
class EngineResult:
    row: Mapping[str, Any]
    bytes_processed: int = 0
    bytes_billed: int = 0
    job_id: str = ""


class SqlEngine(Protocol):
    """Runs one read-only statement that returns exactly one row."""

    def run_one(self, sql: str) -> EngineResult: ...


class RouteDisagreement(Exception):
    """The two independent routes did not produce the same values."""


def normalize(value: Any) -> Scalar:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    raise TypeError(f"unsupported result value type {type(value).__name__}")


def _normalized_row(spec: QuerySpec, row: Mapping[str, Any]) -> dict[str, Scalar]:
    missing = sorted(set(spec.outputs) - set(row))
    if missing:
        raise ValueError(f"missing output columns: {', '.join(missing)}")
    return {name: normalize(row[name]) for name in spec.outputs}


def values_differ(out: OutputSpec, a: Scalar, b: Scalar) -> bool:
    if a is None or b is None:
        return a is not b
    if out.kind in {"money", "share"}:
        return abs(float(a) - float(b)) > out.tol + 1e-9
    if out.kind == "count":
        return int(a) != int(b)
    return a != b


def differences(
    spec: QuerySpec, expected: Mapping[str, Scalar], observed: Mapping[str, Scalar]
) -> list[str]:
    found: list[str] = []
    for name, out in spec.outputs.items():
        if name not in observed:
            found.append(f"{name}: missing")
        elif values_differ(out, expected.get(name), observed[name]):
            found.append(
                f"{name}: expected {expected.get(name)!r}, got {observed[name]!r}"
            )
    return found


class ExpectedQuery(ContractModel):
    primary_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    crosscheck_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    values: Mapping[Identifier, Scalar]


class ExpectedValues(ContractModel):
    schema_version: Literal[1] = 1
    data_ref: Identifier
    extract_digest: str
    spec_digest: str
    computed_with: str
    review: Mapping[str, str]
    queries: Mapping[Identifier, ExpectedQuery]


def compute_expected(
    spec: BenchmarkSpec,
    engine: SqlEngine,
    *,
    dataset_ref: str,
    extract_digest: str,
    computed_with: str,
) -> ExpectedValues:
    """Run both routes of every reference query and require agreement."""
    queries: dict[str, ExpectedQuery] = {}
    for qid, query in spec.queries.items():
        primary_sql = render_sql(query.primary_sql, spec, qid, dataset_ref=dataset_ref)
        cross_sql = render_sql(query.crosscheck_sql, spec, qid, dataset_ref=dataset_ref)
        primary = _normalized_row(query, engine.run_one(primary_sql).row)
        cross = _normalized_row(query, engine.run_one(cross_sql).row)
        problems = differences(query, primary, cross)
        if problems:
            raise RouteDisagreement(f"{qid}: " + "; ".join(problems))
        queries[qid] = ExpectedQuery(
            primary_fingerprint=sql_fingerprint(primary_sql),
            crosscheck_fingerprint=sql_fingerprint(cross_sql),
            values=primary,
        )
    return ExpectedValues(
        data_ref=spec.data_ref,
        extract_digest=extract_digest,
        spec_digest=spec_digest(spec),
        computed_with=computed_with,
        review={
            "routes": "primary and crosscheck statements agree within tolerance",
            "independence": "no compiler, agent or metric-catalog code is involved",
            "human_review": "pending: a named reviewer has not signed off the figures",
        },
        queries=queries,
    )


# ------------------------------------------------------------- manifest


def build_manifest(spec: BenchmarkSpec, expected: ExpectedValues) -> Manifest:
    """Conversation manifest whose literals all come from ``expected``."""
    if expected.spec_digest != spec_digest(spec):
        raise ValueError("expected values were computed for a different spec")
    scenarios = []
    for sc in spec.scenarios:
        scope = spec.scopes[sc.scope]
        expectations: list[Expectation] = []
        for exp in sc.expectations:
            expectations.append(_expectation(exp, expected))
        scenarios.append(
            Scenario(
                id=sc.id,
                title=sc.title,
                level=sc.level,
                category=sc.category,
                mode="fixture",
                importance="threshold",
                implementation_status="implemented",
                verification=("deterministic", "judge")
                if sc.judge
                else ("deterministic",),
                fixture_ref=spec.data_ref,
                scope=ScopeSpec(
                    executive_ref=scope.executive_ref,
                    product_scope=(scope.product_scope,),
                ),
                dialogue=tuple(Turn(text=t) for t in sc.dialogue),
                expectations=tuple(expectations),
                judge=sc.judge,
                requires=REQUIRES,
                tags=tuple(sorted({"realdata", *sc.tags})),
            )
        )
    return Manifest(
        manifest_id=MANIFEST_ID,
        manifest_version=f"{spec.benchmark_version}.{expected.extract_digest[:8]}",
        scenarios=tuple(scenarios),
    )


def _expectation(exp: ExpectationSpec, expected: ExpectedValues) -> Expectation:
    if exp.source is None:
        return ExactExpectation(name=exp.name, expected=exp.value)
    qid, _, column = exp.source.partition(".")
    value = expected.queries[qid].values[column]
    if value is None:
        raise ValueError(f"{exp.source} has no value; the scenario cannot be built")
    if exp.kind == "numeric":
        return NumericExpectation(
            name=exp.name, expected=float(value), abs_tol=exp.abs_tol
        )
    if exp.kind == "exact":
        return ExactExpectation(name=exp.name, expected=value)
    text_kind: Literal["contains", "not_contains"] = (
        "contains" if exp.kind == "contains" else "not_contains"
    )
    return TextExpectation(kind=text_kind, name=exp.name, needle=str(value))


# ---------------------------------------------------------------- drift


class DriftEntry(ContractModel):
    query_id: Identifier
    status: Literal["match", "drift", "error"]
    differences: tuple[str, ...] = ()
    bytes_processed: int = 0
    bytes_billed: int = 0
    job_id: str = ""


class DriftReport(ContractModel):
    """Live source versus the frozen expected values. Never a benchmark verdict."""

    schema_version: Literal[1] = 1
    kind: Literal["live_drift_report"] = "live_drift_report"
    data_ref: Identifier
    extract_digest: str
    observed_at: str
    source_dataset: str
    drifted: bool
    entries: tuple[DriftEntry, ...]
    note: str = (
        "Informational. The benchmark is scored against the frozen extract; a "
        "difference here means the public source changed or was restated."
    )


def drift_report(
    spec: BenchmarkSpec,
    expected: ExpectedValues,
    engine: SqlEngine,
    *,
    dataset_ref: str,
    observed_at: str,
) -> DriftReport:
    entries: list[DriftEntry] = []
    for qid, query in spec.queries.items():
        sql = render_sql(query.primary_sql, spec, qid, dataset_ref=dataset_ref)
        try:
            result = engine.run_one(sql)
            observed = _normalized_row(query, result.row)
        except Exception as exc:  # report, do not abort the remaining queries
            entries.append(
                DriftEntry(
                    query_id=qid, status="error", differences=(type(exc).__name__,)
                )
            )
            continue
        found = differences(query, expected.queries[qid].values, observed)
        entries.append(
            DriftEntry(
                query_id=qid,
                status="drift" if found else "match",
                differences=tuple(found),
                bytes_processed=result.bytes_processed,
                bytes_billed=result.bytes_billed,
                job_id=result.job_id,
            )
        )
    return DriftReport(
        data_ref=expected.data_ref,
        extract_digest=expected.extract_digest,
        observed_at=observed_at,
        source_dataset=spec.extract.source_dataset,
        drifted=any(e.status != "match" for e in entries),
        entries=tuple(entries),
    )


# -------------------------------------------------------------- privacy


def assert_publishable(text: str) -> None:
    """Refuse artifact text that mentions personal-data columns or an e-mail."""
    lowered = text.lower()
    if _EMAIL.search(lowered):
        raise ValueError("artifact is not sanitized: contains an e-mail address")
    for token in FORBIDDEN_TOKENS:
        if token in lowered:
            raise ValueError(f"artifact is not sanitized: mentions {token!r}")


def check_table_columns(table: str, columns: Sequence[str]) -> None:
    allowed = EXTRACT_COLUMNS.get(table)
    if allowed is None or tuple(columns) != allowed:
        raise ValueError(f"extract table {table!r} must have columns {allowed}")
