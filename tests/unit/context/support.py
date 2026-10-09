"""A small in-memory world for context selection and output gate tests.

Evidence is recorded through the real ``EvidenceService`` from real keyed
compiled queries over the DuckDB privacy oracle, so references are genuine
per-executive HMACs and authority stamps come from the resolver.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal

from retail_analytics.adapters.sql_compiler import SqlglotGrainAudit
from retail_analytics.application.authorization import (
    AccessResolver,
    OwnershipGuard,
)
from retail_analytics.application.context import ContextBuilder
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.context import TopicReset
from retail_analytics.application.contracts.evidence import NewEvidence
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.evidence_privacy import EvidencePrivacyScreen
from retail_analytics.application.output_privacy import OutputPrivacyGate
from retail_analytics.application.preferences import PreferenceService
from retail_analytics.application.result_privacy import ReleasedResult
from retail_analytics.application.schema_context import ApprovedSchemaContext
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.context import ContextBudget
from retail_analytics.domain.conversation import Message, MessageRole
from retail_analytics.domain.disclosure import ProtectedTerm
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    AuthorityStamp,
    Evidence,
    EvidenceCell,
    EvidenceColumn,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    Provenance,
    ReusePolicy,
    content_digest,
)
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.runs import RunStatus
from tests.unit.evidence.fakes import Clock, FakeEvidenceStore, Ids
from tests.unit.evidence.support import FINGERPRINT, basis
from tests.unit.preferences.fakes import FakePreferenceStore
from tests.unit.privacy.support import (
    BOUNDARY,
    EXEC_A,
    EXEC_B,
    compile_for,
    customer_database,
    raw_rows,
)
from tests.unit.sql_compiler.support import VERSION, view
from tests.unit.test_authorization import ALL_SCOPES, Directory, Records, access

A = Principal(EXEC_A, ALL_SCOPES)
B = Principal(EXEC_B, ALL_SCOPES)
WIDE = frozenset({"1", "2", "3"})
NARROW = frozenset({"1", "3"})
SPEND_SQL = (
    "SELECT customer_ref, SUM(sale_amount) AS spend FROM sales_items "
    "WHERE sale_amount >= @min_amount GROUP BY customer_ref"
)
BRAND_SQL = (
    "SELECT p.brand, SUM(s.sale_amount) AS revenue FROM sales_items s "
    "JOIN products p ON s.product_id = p.product_id GROUP BY p.brand"
)


@dataclass
class FakeHistory:
    clock: Clock
    messages: list[Message] = field(default_factory=list)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def add(
        self, session_id: str, role: MessageRole, content: str, run_id: str | None
    ) -> Message:
        message = Message(
            f"m{next(self._ids)}", session_id, role, content, self.clock(), run_id
        )
        self.messages.append(message)
        return message

    async def recent_messages(self, session_id: str, limit: int) -> list[Message]:
        own = [m for m in self.messages if m.session_id == session_id]
        return own[-limit:]


@dataclass
class FakeResets:
    clock: Clock
    resets: dict[str, TopicReset] = field(default_factory=dict)

    async def record_reset(self, session_id: str, reset_id: str) -> TopicReset:
        if reset_id not in self.resets:
            self.resets[reset_id] = TopicReset(session_id, reset_id, self.clock())
        return self.resets[reset_id]

    async def latest_reset(self, session_id: str) -> TopicReset | None:
        own = [r for r in self.resets.values() if r.session_id == session_id]
        return max(own, key=lambda r: r.reset_at) if own else None


class World:
    """Executive A (session ``s-a``) and B (``s-b``), runs added per test."""

    def __init__(
        self,
        *,
        budget: ContextBudget | None = None,
        lexicon: tuple[ProtectedTerm, ...] = (),
        schema: ApprovedSchemaContext | None = None,
        id_prefix: str = "evd",
    ) -> None:
        self.clock = Clock()
        self.store = FakeEvidenceStore(clock=self.clock)
        self.evidence = EvidenceService(
            self.store,
            self.store,
            clock=self.clock,
            new_id=Ids(prefix=id_prefix),
            policy=ReusePolicy(),
            # As wired in production: legacy records are re-audited.
            privacy=EvidencePrivacyScreen(audit=SqlglotGrainAudit()),
        )
        self.directory = Directory()
        self.directory.by_id = {
            EXEC_A: access(EXEC_A, set(WIDE), authorization_version=VERSION),
            EXEC_B: access(EXEC_B, {"2"}, authorization_version=VERSION),
        }
        self.records = Records()
        self.records.add(EXEC_A, "a")
        self.records.add(EXEC_B, "b")
        self.guard = OwnershipGuard(self.records, self.records, self.records)
        self.resolver = AccessResolver(self.directory, self.guard)
        self.preference_store = FakePreferenceStore(clock=self.clock)
        self.preferences = PreferenceService(
            self.preference_store,
            self.resolver,
            self.guard,
            default_catalog(),
            self.store,
        )
        self.history = FakeHistory(self.clock)
        self.resets = FakeResets(self.clock)
        self.builder = ContextBuilder(
            self.resolver,
            self.guard,
            self.history,
            self.evidence,
            self.preferences,
            self.resets,
            budget=budget,
            protected_terms=lambda: lexicon,
            schema=schema,
        )
        self.gate = OutputPrivacyGate(
            self.resolver,
            self.evidence,
            self.history,
            protected_terms=lambda: lexicon,
        )
        self.db = customer_database()
        self._ops = itertools.count(1)

    def tick(self, minutes: int = 1) -> None:
        self.clock.advance(timedelta(minutes=minutes))

    def new_run(self, executive: str = EXEC_A, session: str = "s-a") -> str:
        run_id = f"r{len(self.records.runs) + 1}"
        template = self.records.runs["r-a"]
        self.records.runs[run_id] = replace(
            template,
            run_id=run_id,
            session_id=session,
            requested_by=executive,
            status=RunStatus.RUNNING,
        )
        return run_id

    def set_products(self, executive: str, products: frozenset[str]) -> None:
        """An administrator changes entitlements: the version always increases."""
        current = self.directory.by_id[executive]
        self.directory.by_id[executive] = replace(
            current,
            product_ids=products,
            authorization_version=current.authorization_version + 1,
        )

    def scope(self, executive: str = EXEC_A) -> ProductScope:
        return self.directory.by_id[executive].product_scope

    async def query(
        self,
        run_id: str,
        sql: str = SPEND_SQL,
        principal: Principal = A,
        values: dict[str, object] | None = None,
        grain: tuple[str, ...] | None = None,
    ) -> tuple[Evidence, ReleasedResult]:
        ctx = await self.resolver.context_for_run(principal, run_id)
        scope = ctx.product_scope
        compiled = compile_for(
            principal.executive_id,
            sql,
            scope,
            {"min_amount": 1} if values is None and "@min_amount" in sql else values,  # type: ignore[arg-type]
        )
        released = BOUNDARY.release(
            compiled,
            raw_rows(self.db, compiled),
            catalog=view(version=scope.entitlement_version),
        )
        if grain is None:
            grain = ("customer_ref",) if "customer_ref" in sql else ("brand",)
        evidence = await self.evidence.record_query(
            OperationContext(ctx, f"op{next(self._ops)}"),
            compiled,
            released,
            basis(grain=grain),
        )
        return evidence, released

    async def external(
        self,
        run_id: str,
        rows: tuple[tuple[str | Decimal | int | None, ...], ...],
        columns: tuple[str, ...] = ("label", "amount"),
        principal: Principal = A,
        truncation: str | None = None,
    ) -> Evidence:
        """Evidence with arbitrary content (e.g. a provider's text) for fault tests."""
        ctx = await self.resolver.context_for_run(principal, run_id)
        content = EvidenceContent(
            kind=EvidenceKind.EXTERNAL,
            subject_key=f"x:{next(self._ops)}",
            analysis=AnalysisStamp(
                catalog_version=1,
                policy_version=2,
                definitions=frozenset(),
                preference_fingerprint=FINGERPRINT,
            ),
            provenance=Provenance(notes=(("source", "fixture"),)),
            table=EvidenceTable(
                columns=tuple(EvidenceColumn(c, "value") for c in columns),
                rows=rows,
                received_rows=len(rows),
                truncation=truncation,
            ),
            grain=(),
            analytical_slots=frozenset({"metric_definition:revenue"}),
        )
        return await self.evidence.record(
            OperationContext(ctx, f"op{next(self._ops)}"), content
        )

    async def legacy(
        self,
        run_id: str,
        *,
        sql: str | None,
        columns: tuple[EvidenceColumn, ...],
        rows: tuple[tuple[EvidenceCell, ...], ...],
        relations: tuple[str, ...] = ("customers",),
        kind: EvidenceKind = EvidenceKind.QUERY,
        derived_from: tuple[str, ...] = (),
        principal: Principal = A,
    ) -> Evidence:
        """A record as stored before demographics became aggregate-only
        (result policy version 1), written straight to the store."""
        ctx = await self.resolver.context_for_run(principal, run_id)
        n = next(self._ops)
        query = kind is EvidenceKind.QUERY
        content = EvidenceContent(
            kind=kind,
            subject_key=f"q:legacy-{n}",
            analysis=AnalysisStamp(
                catalog_version=1,
                policy_version=1,
                definitions=frozenset(),
                preference_fingerprint=FINGERPRINT,
            ),
            provenance=Provenance(
                logical_sql=sql,
                relations=relations,
                executed_query_digest="0" * 64,
            )
            if query
            else Provenance(notes=(("kind", "currency_conversion"),)),
            table=EvidenceTable(columns=columns, rows=rows, received_rows=len(rows)),
            grain=(),
            derived_from=derived_from,
        )
        when = self.clock()
        return await self.store.record(
            NewEvidence(
                evidence_id=f"evdlegacy{n}",
                executive_id=ctx.executive_id,
                session_id=ctx.correlation.session_id,
                run_id=run_id,
                operation_id=f"op{n}",
                authority=AuthorityStamp.of(ctx.product_scope),
                content=content,
                computed_at=when,
                content_digest=content_digest(content, when),
                scope_products=ctx.product_scope.product_ids,
            )
        )

    def say(
        self, role: MessageRole, content: str, run_id: str | None, session: str = "s-a"
    ) -> Message:
        return self.history.add(session, role, content, run_id)


def references_of(released: ReleasedResult) -> list[str]:
    return [str(r["customer_ref"]) for r in released.records()]
