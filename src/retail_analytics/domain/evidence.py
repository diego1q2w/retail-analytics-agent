"""Immutable analytical evidence and the rules for reusing it.

Evidence is the supporting material behind a finding: bounded, already
privacy-released rows plus how they were obtained (logical query, analysis
parameters, definitions, catalog, policy and authorization versions, period
and computation time). A stored record never changes; a refresh produces a new
version in the same lineage, so a report keeps citing the snapshot it used.

Reuse is a privacy boundary as well as a correctness rule. ``ReusePolicy``
decides, for one candidate and one request, whether the stored rows may stand
in for a new query. Authority is checked first (owner, session, non-empty and
unchanged product scope), then meaning (catalog, policy, definitions,
preference fingerprint, period, time zone), then sufficiency (freshness for
current-data questions, granularity, truncation). Anything else recomputes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.periods import DEFAULT_TIME_ZONE, DateWindow

DEFAULT_CURRENT_FRESHNESS = timedelta(minutes=15)
# Encoded rows; larger results belong in an artifact, not in the database row.
MAX_PAYLOAD_BYTES = 512 * 1024
# Logical query, parameters and source-specific notes.
MAX_PROVENANCE_BYTES = 64 * 1024
MAX_SUBJECT_KEY = 128

type EvidenceCell = str | int | float | bool | Decimal | date | datetime | None
type JsonValue = (
    str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
)


class EvidenceError(ValueError):
    """An evidence record or request is malformed (a programming error)."""


class EvidenceKind(StrEnum):
    # Rows released from a warehouse query.
    QUERY = "query"
    # Calculated from other evidence (listed in ``derived_from``).
    DERIVED = "derived"
    # From an external source such as an exchange-rate provider.
    EXTERNAL = "external"


class EvidenceUse(StrEnum):
    PRODUCED = "produced"
    REUSED = "reused"


@dataclass(frozen=True, slots=True, order=True)
class DefinitionRef:
    """A metric (or reviewed exploratory definition) at one version."""

    metric_id: str
    version: int

    def __post_init__(self) -> None:
        if not self.metric_id or self.version < 1:
            raise EvidenceError("definition needs an id and version >= 1")

    def __str__(self) -> str:
        return f"{self.metric_id}@{self.version}"


@dataclass(frozen=True, slots=True, order=True)
class TermMeaning:
    """Which definition a business term (e.g. "revenue") resolved to when the
    evidence was computed (preference or shared default, trusted code)."""

    term: str
    definition: DefinitionRef

    def __post_init__(self) -> None:
        if not self.term:
            raise EvidenceError("term meaning needs a term")


def product_set_digest(product_ids: Iterable[str]) -> str:
    """Stable digest of an exact product set: SHA-256 of the sorted IDs joined
    by newlines (PostgreSQL checks stored scope snapshots the same way)."""
    joined = "\n".join(sorted(set(product_ids)))
    return hashlib.sha256(joined.encode()).hexdigest()


def scope_digest(scope: ProductScope) -> str:
    """Stable digest of the exact product set (the IDs themselves are not kept)."""
    return product_set_digest(scope.product_ids)


@dataclass(frozen=True, slots=True)
class AuthorityStamp:
    """The authority the evidence was computed under, stamped by trusted code."""

    authorization_version: int
    scope_digest: str

    @classmethod
    def of(cls, scope: ProductScope) -> AuthorityStamp:
        if scope.is_empty:
            raise EvidenceError("empty product scope cannot produce evidence")
        return cls(scope.entitlement_version, scope_digest(scope))

    def matches(self, scope: ProductScope) -> bool:
        return (
            not scope.is_empty
            and scope.entitlement_version == self.authorization_version
            and scope_digest(scope) == self.scope_digest
        )


@dataclass(frozen=True, slots=True)
class AnalysisStamp:
    """What the numbers mean: versions that must match for reuse.

    ``definitions_recorded`` says trusted code recorded the definition basis
    (``definitions``, ``terms``, ``date_basis``) at computation time. The basis
    is analytical context: the definitions relevant to the fields the query
    read. It does not prove the SQL calculated that metric. ``period`` is the
    one exact window, or None: a query comparing several periods records no
    single period. Records
    made before that (or by code that does not know it) leave it False: their
    definitions are *unknown*, never assumed compatible with current ones.
    """

    catalog_version: int
    policy_version: int
    definitions: frozenset[DefinitionRef]
    # ``EffectivePreferences.analytical_fingerprint`` at computation time.
    preference_fingerprint: str
    period: DateWindow | None = None
    time_zone: str = DEFAULT_TIME_ZONE
    terms: frozenset[TermMeaning] = frozenset()
    # Logical date field the rows are dated by (e.g. ``ordered_date``).
    date_basis: str | None = None
    definitions_recorded: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceParameter:
    """A model-supplied analysis value (never a trusted or secret parameter)."""

    name: str
    type: str
    value: JsonValue


@dataclass(frozen=True, slots=True)
class Provenance:
    """How the evidence was obtained.

    ``executed_query_digest`` fingerprints the protected physical statement;
    the statement itself stays with the query execution record. ``notes`` hold
    small source-specific facts (for example rate source, date and method).
    """

    logical_sql: str | None = None
    parameters: tuple[EvidenceParameter, ...] = ()
    relations: tuple[str, ...] = ()
    executed_query_digest: str | None = None
    notes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceColumn:
    name: str
    # Privacy role of the column ("reference", "age_band" or "value").
    role: str
    # Logical source fields as "relation.field".
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceTable:
    """Released rows. ``truncated`` means rows are missing from the result."""

    columns: tuple[EvidenceColumn, ...]
    rows: tuple[tuple[EvidenceCell, ...], ...]
    received_rows: int
    truncation: str | None = None
    masked_cells: int = 0

    def __post_init__(self) -> None:
        width = len(self.columns)
        if len({c.name for c in self.columns}) != width:
            raise EvidenceError("column names must be unique")
        if any(len(row) != width for row in self.rows):
            raise EvidenceError("every row needs one value per column")

    @property
    def truncated(self) -> bool:
        return self.truncation is not None

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def __repr__(self) -> str:
        return (
            f"EvidenceTable(columns={list(self.column_names)}, "
            f"rows=<{len(self.rows)} rows>, truncation={self.truncation})"
        )


@dataclass(frozen=True, slots=True)
class EvidenceContent:
    """Everything that defines a record's meaning; covered by its digest."""

    kind: EvidenceKind
    # Identifies "the same question" for reuse lookups (e.g. logical query hash).
    subject_key: str
    analysis: AnalysisStamp
    provenance: Provenance
    table: EvidenceTable
    # Output columns the rows are grouped by; reuse needs requested grain here.
    grain: tuple[str, ...]
    # Analytical preference slots the computation depended on (for invalidation).
    analytical_slots: frozenset[str] = frozenset()
    derived_from: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 < len(self.subject_key) <= MAX_SUBJECT_KEY:
            raise EvidenceError("subject_key must be 1-128 characters")
        if not set(self.grain) <= set(self.table.column_names):
            raise EvidenceError("grain must name result columns")
        if self.kind is EvidenceKind.QUERY and not (
            self.provenance.logical_sql and self.provenance.executed_query_digest
        ):
            raise EvidenceError("query evidence needs its logical query and digest")
        if self.kind is EvidenceKind.DERIVED and not self.derived_from:
            raise EvidenceError("derived evidence must name its inputs")


@dataclass(frozen=True, slots=True)
class Evidence:
    """One immutable, owned evidence version."""

    evidence_id: str
    lineage_id: str
    version: int
    executive_id: str
    session_id: str
    run_id: str
    operation_id: str
    authority: AuthorityStamp
    content: EvidenceContent
    computed_at: datetime
    content_digest: str

    def __post_init__(self) -> None:
        if self.version < 1:
            raise EvidenceError("version starts at 1")
        if self.computed_at.tzinfo is None:
            raise EvidenceError("computed_at must be timezone-aware")

    @property
    def is_intact(self) -> bool:
        """The stored digest still matches the stored content."""
        return self.content_digest == content_digest(self.content, self.computed_at)

    def __repr__(self) -> str:
        return (
            f"Evidence({self.evidence_id!r}, lineage={self.lineage_id!r}, "
            f"v{self.version}, kind={self.content.kind.value})"
        )


@dataclass(frozen=True, slots=True)
class PinHolder:
    """Something that retains evidence beyond the investigation (a report)."""

    kind: str
    holder_id: str

    def __post_init__(self) -> None:
        if not self.kind or not self.holder_id:
            raise EvidenceError("pin holder needs a kind and id")


@dataclass(frozen=True, slots=True)
class ReportSource:
    """Where evidence linked into another session came from: one of the
    owner's saved report versions (a historical snapshot, never refreshed).

    ``title`` is the saved title (it passed the REPORT output check when the
    version was saved); ``imported_at`` is when it entered the session.
    """

    report_id: str
    version: int
    title: str
    imported_at: datetime

    def __post_init__(self) -> None:
        if not self.report_id or self.version < 1:
            raise EvidenceError("report source needs an id and version >= 1")
        if self.imported_at.tzinfo is None:
            raise EvidenceError("imported_at must be timezone-aware")

    def describe(self, evidence: Evidence) -> str:
        """Provenance line shown wherever the reused figures appear."""
        analysis = evidence.content.analysis
        when = evidence.computed_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        parts = [f'from saved report "{self.title}" (v{self.version})']
        parts.append(f"computed {when}")
        if analysis.period is not None:
            parts.append(f"period {analysis.period.describe()}")
        if analysis.definitions:
            parts.append(
                "definitions " + ", ".join(str(d) for d in sorted(analysis.definitions))
            )
        parts.append(f"catalog v{analysis.catalog_version}")
        return ", ".join(parts) + "; historical snapshot, not current data"


# --- Reuse policy -----------------------------------------------------------


class ReuseIntent(StrEnum):
    # Explain or re-present an earlier result: the original snapshot, any age.
    EXPLAIN = "explain"
    # A question about current data: freshness limit applies.
    CURRENT = "current"
    # Explicit refresh: always query again.
    REFRESH = "refresh"


class ReuseBlock(StrEnum):
    """Why a candidate cannot be reused (first failing rule)."""

    NOT_OWNED = "not_owned"
    OTHER_SESSION = "other_session"
    NO_PRODUCT_SCOPE = "no_product_scope"
    AUTHORIZATION_CHANGED = "authorization_changed"
    TAMPERED = "tampered"
    INVALIDATED = "invalidated"
    # Reached only through saved reports that were soft-deleted (or restored
    # but not yet re-validated): reuse through them stopped with the deletion.
    REPORT_LINK_WITHDRAWN = "report_link_withdrawn"
    # Computed under an earlier privacy policy (customer demographics shown
    # per customer) and not verifiably group-level: never shown or reused.
    PRIVACY_POLICY_WITHDRAWN = "privacy_policy_withdrawn"
    CATALOG_CHANGED = "catalog_changed"
    POLICY_CHANGED = "policy_changed"
    DEFINITIONS_CHANGED = "definitions_changed"
    # The record does not say which definitions it used: compatibility is
    # unknown, so it is recomputed rather than reused (never "compatible").
    DEFINITIONS_UNKNOWN = "definitions_unknown"
    PREFERENCES_CHANGED = "preferences_changed"
    PERIOD_MISMATCH = "period_mismatch"
    TIME_ZONE_MISMATCH = "time_zone_mismatch"
    REFRESH_REQUESTED = "refresh_requested"
    STALE = "stale"
    INSUFFICIENT_GRANULARITY = "insufficient_granularity"
    TRUNCATED = "truncated"

    @property
    def is_authority(self) -> bool:
        return self in _AUTHORITY_BLOCKS


_AUTHORITY_BLOCKS = frozenset(
    {
        ReuseBlock.NOT_OWNED,
        ReuseBlock.OTHER_SESSION,
        ReuseBlock.NO_PRODUCT_SCOPE,
        ReuseBlock.AUTHORIZATION_CHANGED,
        ReuseBlock.TAMPERED,
        ReuseBlock.INVALIDATED,
        ReuseBlock.REPORT_LINK_WITHDRAWN,
        ReuseBlock.PRIVACY_POLICY_WITHDRAWN,
    }
)


@dataclass(frozen=True, slots=True)
class CurrentAuthority:
    """Freshly resolved authority of the caller (never from stored evidence)."""

    executive_id: str
    session_id: str
    scope: ProductScope


@dataclass(frozen=True, slots=True)
class Requirements:
    """What the new calculation would use; reuse needs the same meaning.

    ``definitions`` must all be present in the evidence at the same versions.
    ``grain`` lists columns the answer must be broken down by.
    ``requires_complete`` is set when the rows feed a calculation, so a
    truncated result cannot stand in for the full one.
    """

    catalog_version: int
    policy_version: int
    preference_fingerprint: str
    definitions: frozenset[DefinitionRef] = frozenset()
    period: DateWindow | None = None
    time_zone: str = DEFAULT_TIME_ZONE
    grain: frozenset[str] = frozenset()
    requires_complete: bool = False


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Disclosure for reused evidence: when it was computed and for what period."""

    evidence_id: str
    version: int
    computed_at: datetime
    age: timedelta
    period: DateWindow | None

    def describe(self) -> str:
        when = self.computed_at.strftime("%Y-%m-%d %H:%M UTC")
        span = f" for {self.period.describe()}" if self.period else ""
        return f"Snapshot computed at {when}{span}; source data may have changed since."


@dataclass(frozen=True, slots=True)
class ReusePolicy:
    current_freshness: timedelta = DEFAULT_CURRENT_FRESHNESS

    def __post_init__(self) -> None:
        if self.current_freshness < timedelta(0):
            raise EvidenceError("freshness must not be negative")

    def authority_block(
        self,
        evidence: Evidence,
        authority: CurrentAuthority,
        *,
        invalidated: bool,
        source_withdrawn: bool = False,
        privacy_withdrawn: bool = False,
    ) -> ReuseBlock | None:
        """Checks that apply to any use of stored evidence, including context.

        ``source_withdrawn``: the record was derived from saved-report
        evidence that no longer has a valid link into its session.
        ``privacy_withdrawn``: the record fails the current privacy policy
        (legacy individual-level demographics; see ``EvidencePrivacyScreen``).
        """
        if evidence.executive_id != authority.executive_id:
            return ReuseBlock.NOT_OWNED
        if evidence.session_id != authority.session_id:
            return ReuseBlock.OTHER_SESSION
        if authority.scope.is_empty:
            return ReuseBlock.NO_PRODUCT_SCOPE
        if not evidence.authority.matches(authority.scope):
            return ReuseBlock.AUTHORIZATION_CHANGED
        if not evidence.is_intact:
            return ReuseBlock.TAMPERED
        if privacy_withdrawn:
            return ReuseBlock.PRIVACY_POLICY_WITHDRAWN
        if source_withdrawn:
            return ReuseBlock.REPORT_LINK_WITHDRAWN
        if invalidated:
            return ReuseBlock.INVALIDATED
        return None

    def imported_block(
        self,
        evidence: Evidence,
        authority: CurrentAuthority,
        *,
        covered: bool,
        invalidated: bool,
        withdrawn: bool = False,
        privacy_withdrawn: bool = False,
    ) -> ReuseBlock | None:
        """Authority for the owner's report evidence linked into another of
        their sessions (T18-F1 rule, no extra grant): same owner, and the
        current products cover the exact set it was computed under
        (``covered``, judged from trusted scope snapshots). Widened access
        keeps it usable; losing any required product withholds it.
        ``withdrawn``: every report it was linked through was soft-deleted
        (or restored and not yet re-validated)."""
        if evidence.executive_id != authority.executive_id:
            return ReuseBlock.NOT_OWNED
        if authority.scope.is_empty:
            return ReuseBlock.NO_PRODUCT_SCOPE
        if not covered:
            return ReuseBlock.AUTHORIZATION_CHANGED
        if not evidence.is_intact:
            return ReuseBlock.TAMPERED
        if privacy_withdrawn:
            return ReuseBlock.PRIVACY_POLICY_WITHDRAWN
        if withdrawn:
            return ReuseBlock.REPORT_LINK_WITHDRAWN
        if invalidated:
            return ReuseBlock.INVALIDATED
        return None

    def assess(
        self,
        evidence: Evidence,
        *,
        authority: CurrentAuthority,
        requirements: Requirements,
        intent: ReuseIntent,
        now: datetime,
        invalidated: bool = False,
        source_withdrawn: bool = False,
        privacy_withdrawn: bool = False,
    ) -> ReuseBlock | None:
        """``None`` means the evidence may be reused for this request."""
        block = self.authority_block(
            evidence,
            authority,
            invalidated=invalidated,
            source_withdrawn=source_withdrawn,
            privacy_withdrawn=privacy_withdrawn,
        )
        if block is not None:
            return block
        return self.suitability(
            evidence, requirements=requirements, intent=intent, now=now
        )

    def suitability(
        self,
        evidence: Evidence,
        *,
        requirements: Requirements,
        intent: ReuseIntent,
        now: datetime,
    ) -> ReuseBlock | None:
        """Meaning, freshness and sufficiency, once authority has passed.

        Applies unchanged to report evidence linked from another session: a
        question about current data still needs a record younger than the
        freshness limit, so an old report snapshot is recomputed.
        """
        block = _meaning_block(evidence.content, requirements)
        if block is not None:
            return block
        if intent is ReuseIntent.REFRESH:
            return ReuseBlock.REFRESH_REQUESTED
        if intent is ReuseIntent.CURRENT and (
            age(evidence, now) > self.current_freshness
        ):
            return ReuseBlock.STALE
        if not requirements.grain <= set(evidence.content.grain):
            return ReuseBlock.INSUFFICIENT_GRANULARITY
        if requirements.requires_complete and evidence.content.table.truncated:
            return ReuseBlock.TRUNCATED
        return None


@dataclass(frozen=True, slots=True)
class AnalysisCompatibility:
    """The meaning a reused historical record must still have *now*.

    ``current_definitions`` maps each metric to its current version; a record
    computed under an older (or unknown) version is not reused as a result.
    ``preference_fingerprint`` is the caller's current effective analytical
    fingerprint for the session the record would be reused in.
    """

    catalog_version: int
    policy_version: int
    preference_fingerprint: str
    current_definitions: Mapping[str, int]


def compatibility_block(
    content: EvidenceContent, compatibility: AnalysisCompatibility
) -> ReuseBlock | None:
    """Whether a record's meaning still matches current definitions/settings.

    A record without recorded definitions is not compatible: unknown is never
    treated as unchanged.
    """
    stamp = content.analysis
    if not stamp.definitions_recorded:
        return ReuseBlock.DEFINITIONS_UNKNOWN
    if stamp.catalog_version != compatibility.catalog_version:
        return ReuseBlock.CATALOG_CHANGED
    if stamp.policy_version != compatibility.policy_version:
        return ReuseBlock.POLICY_CHANGED
    if any(
        compatibility.current_definitions.get(d.metric_id) != d.version
        for d in stamp.definitions
    ):
        return ReuseBlock.DEFINITIONS_CHANGED
    if stamp.preference_fingerprint != compatibility.preference_fingerprint:
        return ReuseBlock.PREFERENCES_CHANGED
    return None


def _meaning_block(content: EvidenceContent, req: Requirements) -> ReuseBlock | None:
    stamp = content.analysis
    if stamp.catalog_version != req.catalog_version:
        return ReuseBlock.CATALOG_CHANGED
    if stamp.policy_version != req.policy_version:
        return ReuseBlock.POLICY_CHANGED
    if not req.definitions <= stamp.definitions:
        return ReuseBlock.DEFINITIONS_CHANGED
    if stamp.preference_fingerprint != req.preference_fingerprint:
        return ReuseBlock.PREFERENCES_CHANGED
    if req.period is not None and stamp.period != req.period:
        return ReuseBlock.PERIOD_MISMATCH
    if stamp.time_zone != req.time_zone:
        return ReuseBlock.TIME_ZONE_MISMATCH
    return None


def age(evidence: Evidence, now: datetime) -> timedelta:
    # A record from the future (clock skew) counts as just computed.
    return max(now - evidence.computed_at, timedelta(0))


def snapshot(evidence: Evidence, now: datetime) -> Snapshot:
    return Snapshot(
        evidence.evidence_id,
        evidence.version,
        evidence.computed_at,
        age(evidence, now),
        evidence.content.analysis.period,
    )


# --- Canonical encoding (storage and digest) ---------------------------------

_DECIMAL = "$dec"
_DATE = "$date"
_DATETIME = "$ts"
_FLOAT = "$float"
_TAGS = frozenset({_DECIMAL, _DATE, _DATETIME, _FLOAT})


def encode_cell(cell: EvidenceCell) -> JsonValue:
    """JSON form of a cell that round-trips exactly through JSONB."""
    if cell is None or isinstance(cell, bool | str | int):
        return cell
    if isinstance(cell, float):
        if not math.isfinite(cell):
            return {_FLOAT: repr(cell)}
        return 0.0 if cell == 0 else cell  # no negative zero in JSONB numerics
    if isinstance(cell, Decimal):
        return {_DECIMAL: str(cell)}
    if isinstance(cell, datetime):
        if cell.tzinfo is None:
            raise EvidenceError("timestamps in evidence must be timezone-aware")
        return {_DATETIME: cell.isoformat()}
    if isinstance(cell, date):
        return {_DATE: cell.isoformat()}
    raise EvidenceError(f"unsupported cell type {type(cell).__name__}")


def decode_cell(value: JsonValue) -> EvidenceCell:
    if value is None or isinstance(value, bool | str | int | float):
        return value
    if isinstance(value, dict) and len(value) == 1:
        ((tag, text),) = value.items()
        if tag in _TAGS and isinstance(text, str):
            if tag == _DECIMAL:
                return Decimal(text)
            if tag == _DATETIME:
                return datetime.fromisoformat(text)
            if tag == _DATE:
                return date.fromisoformat(text)
            return float(text)
    raise EvidenceError("malformed evidence cell")


def encode_table(table: EvidenceTable) -> dict[str, JsonValue]:
    return {
        "columns": [
            {"name": c.name, "role": c.role, "sources": [x for x in c.sources]}
            for c in table.columns
        ],
        "rows": [[encode_cell(cell) for cell in row] for row in table.rows],
        "received_rows": table.received_rows,
        "truncation": table.truncation,
        "masked_cells": table.masked_cells,
    }


def decode_table(data: Mapping[str, object]) -> EvidenceTable:
    columns = _as_list(data["columns"])
    rows = _as_list(data["rows"])
    return EvidenceTable(
        columns=tuple(
            EvidenceColumn(
                str(c["name"]),
                str(c["role"]),
                tuple(str(s) for s in _as_list(c["sources"])),
            )
            for c in (_as_map(item) for item in columns)
        ),
        rows=tuple(
            tuple(decode_cell(_json(cell)) for cell in _as_list(row)) for row in rows
        ),
        received_rows=_as_int(data["received_rows"]),
        truncation=None if data["truncation"] is None else str(data["truncation"]),
        masked_cells=_as_int(data["masked_cells"]),
    )


def encode_provenance(provenance: Provenance) -> dict[str, JsonValue]:
    return {
        "logical_sql": provenance.logical_sql,
        "parameters": [
            {"name": p.name, "type": p.type, "value": p.value}
            for p in provenance.parameters
        ],
        "relations": [r for r in provenance.relations],
        "executed_query_digest": provenance.executed_query_digest,
        "notes": [[k, v] for k, v in provenance.notes],
    }


def decode_provenance(data: Mapping[str, object]) -> Provenance:
    sql = data["logical_sql"]
    digest = data["executed_query_digest"]
    return Provenance(
        logical_sql=None if sql is None else str(sql),
        parameters=tuple(
            EvidenceParameter(str(p["name"]), str(p["type"]), _json(p["value"]))
            for p in (_as_map(item) for item in _as_list(data["parameters"]))
        ),
        relations=tuple(str(r) for r in _as_list(data["relations"])),
        executed_query_digest=None if digest is None else str(digest),
        notes=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in (_as_list(item) for item in _as_list(data["notes"]))
        ),
    )


def encode_analysis(stamp: AnalysisStamp) -> dict[str, JsonValue]:
    period = stamp.period
    document: dict[str, JsonValue] = {
        "catalog_version": stamp.catalog_version,
        "policy_version": stamp.policy_version,
        "definitions": [[d.metric_id, d.version] for d in sorted(stamp.definitions)],
        "preference_fingerprint": stamp.preference_fingerprint,
        "period": None
        if period is None
        else [period.start.isoformat(), period.end.isoformat()],
        "time_zone": stamp.time_zone,
    }
    # Only present when recorded, so records made before the definition basis
    # existed keep their exact encoding (and content digest).
    if stamp.definitions_recorded:
        document["definitions_recorded"] = True
        document["terms"] = [
            [t.term, t.definition.metric_id, t.definition.version]
            for t in sorted(stamp.terms)
        ]
        document["date_basis"] = stamp.date_basis
    return document


def decode_analysis(data: Mapping[str, object]) -> AnalysisStamp:
    period = data["period"]
    basis = data.get("date_basis")
    window = None
    if period is not None:
        start, end = _as_list(period)
        window = DateWindow(
            date.fromisoformat(str(start)), date.fromisoformat(str(end))
        )
    return AnalysisStamp(
        catalog_version=_as_int(data["catalog_version"]),
        policy_version=_as_int(data["policy_version"]),
        definitions=frozenset(
            DefinitionRef(str(pair[0]), _as_int(pair[1]))
            for pair in (_as_list(item) for item in _as_list(data["definitions"]))
        ),
        preference_fingerprint=str(data["preference_fingerprint"]),
        period=window,
        time_zone=str(data["time_zone"]),
        terms=frozenset(
            TermMeaning(str(item[0]), DefinitionRef(str(item[1]), _as_int(item[2])))
            for item in (_as_list(x) for x in _as_list(data.get("terms", [])))
        ),
        date_basis=None if basis is None else str(basis),
        definitions_recorded=data.get("definitions_recorded") is True,
    )


def canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_digest(content: EvidenceContent, computed_at: datetime) -> str:
    document: dict[str, JsonValue] = {
        "kind": content.kind.value,
        "subject_key": content.subject_key,
        "analysis": encode_analysis(content.analysis),
        "provenance": encode_provenance(content.provenance),
        "table": encode_table(content.table),
        "grain": [g for g in content.grain],
        "analytical_slots": [s for s in sorted(content.analytical_slots)],
        "derived_from": [d for d in content.derived_from],
        "computed_at": computed_at.astimezone(UTC).isoformat(),
    }
    return hashlib.sha256(canonical_json(document).encode()).hexdigest()


def encoded_size(value: JsonValue) -> int:
    return len(canonical_json(value).encode())


def check_bounds(content: EvidenceContent) -> None:
    """Reject content that does not fit a bounded database payload."""
    if encoded_size(encode_table(content.table)) > MAX_PAYLOAD_BYTES:
        raise EvidenceError("evidence rows exceed the stored payload limit")
    if encoded_size(encode_provenance(content.provenance)) > MAX_PROVENANCE_BYTES:
        raise EvidenceError("evidence provenance exceeds its size limit")


def _json(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    raise EvidenceError("malformed evidence document")


def _as_list(value: object) -> Sequence[object]:
    if not isinstance(value, list):
        raise EvidenceError("malformed evidence document")
    return value


def _as_map(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise EvidenceError("malformed evidence document")
    return value


def _as_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise EvidenceError("malformed evidence document")
    return value
