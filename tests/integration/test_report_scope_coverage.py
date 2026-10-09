"""Report access by required-scope coverage (T18-F1) on real PostgreSQL (Docker).

A saved version stays readable while the owner's current products cover the
exact product sets its cited evidence was computed under (stamped by trusted
code, kept as scope snapshots). Covers widening, required and unrelated
narrowing, deletion previews, model-supplied and row-derived scope, legacy
versions, and the migration backfill.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.reports import ReportAccess
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.tools import (
    CapabilityRegistry,
    ToolCall,
    ToolFailed,
    ToolSucceeded,
)
from retail_analytics.application.tools.gateway import invoke
from retail_analytics.bootstrap.report_deletion import build_report_deletion
from retail_analytics.capabilities.reports import report_capabilities
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    Provenance,
    product_set_digest,
)
from retail_analytics.domain.operations import SideEffect, ToolErrorCode
from retail_analytics.domain.periods import DateWindow
from retail_analytics.domain.reports import ReportError, ReportErrorCode
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.test_reports import Env
from tests.unit.evidence.support import FINGERPRINT, REVENUE
from tests.unit.reports.support import PRODUCT_COLUMNS, draft
from tests.unit.tools.fakes import RecordingSink

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]

BASE = {"1", "2", "3"}
# Rows name only product 1: the evidence was still computed under 1-3.
ONE_PRODUCT_ROWS: tuple[tuple[object, ...], ...] = ((1, "Shirt", "Zed", 30),)


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class MemoryPrincipals:
    by_run: dict[str, Principal] = field(default_factory=dict)

    async def record(self, run_id: str, principal: Principal) -> Principal:
        return self.by_run.setdefault(run_id, principal)

    async def get(self, run_id: str) -> Principal | None:
        return self.by_run.get(run_id)


class ScopeEnv(Env):
    def __init__(self, stack: Stack, root: Path) -> None:
        super().__init__(stack, root)
        self.deletion = build_report_deletion(self.db, self.access.resolver)

    async def evidence_with_rows(
        self, principal: Principal, run_id: str, rows: tuple[tuple[object, ...], ...]
    ) -> str:
        ctx = await self.access.resolver.context_for_run(principal, run_id)
        op = await self.db.tool_executions.begin(
            OperationRequest(
                operation_id=_id("op"),
                run_id=run_id,
                capability="execute_analysis",
                capability_version=1,
                side_effect=SideEffect.EXTERNAL_JOB,
            )
        )
        content = EvidenceContent(
            kind=EvidenceKind.EXTERNAL,
            subject_key=_id("p"),
            analysis=AnalysisStamp(
                1,
                1,
                frozenset({REVENUE}),
                FINGERPRINT,
                period=DateWindow(date(2026, 9, 1), date(2026, 10, 1)),
            ),
            provenance=Provenance(notes=(("source", "fixture"),)),
            table=EvidenceTable(
                columns=PRODUCT_COLUMNS,
                rows=rows,  # type: ignore[arg-type]
                received_rows=len(rows),
            ),
            grain=("product_id",),
        )
        record = await self.evidence.record(
            OperationContext(ctx, op.execution.operation_id), content
        )
        return record.evidence_id

    async def save(
        self, principal: Principal, session: str, *, rows: Any = None
    ) -> tuple[str, str]:
        run = await self.run(principal, session)
        evidence_id = (
            await self.product_evidence(principal, run)
            if rows is None
            else await self.evidence_with_rows(principal, run, rows)
        )
        saved = await self.reports.create(
            principal, run, draft(evidence_id), operation_id=_id("op")
        )
        return saved.version.report_id, run

    async def entitle(self, principal: Principal, products: set[str]) -> None:
        await self.db.access_admin.replace_products(principal.executive_id, products)

    async def readable(
        self, principal: Principal, session: str, report_id: str
    ) -> bool:
        """Read, export, list, versions, search and deletion preview agree."""
        (listing,) = await self.reports.list_reports(principal)
        versions = await self.reports.versions(principal, report_id)
        search = await self.reports.search(principal, "September")
        run = await self.run(principal, session)
        ctx = await self.access.resolver.context_for_run(principal, run)
        preview = await self.deletion.propose(
            OperationContext(ctx, _id("op")), (report_id,)
        )
        await self.deletion.cancel(principal, preview.proposal_id)
        try:
            await self.reports.read(principal, report_id)
            await self.reports.export(principal, report_id)
        except ReportError as error:
            assert error.code is ReportErrorCode.ACCESS_CHANGED
            assert listing.title is None
            assert listing.access is ReportAccess.ACCESS_CHANGED
            assert all(v.access is ReportAccess.ACCESS_CHANGED for v in versions)
            assert search.matches == () and search.withheld == 1
            assert preview.items[0].title is None
            return False
        assert listing.access is ReportAccess.AVAILABLE and listing.title
        assert all(v.access is ReportAccess.AVAILABLE for v in versions)
        assert len(search.matches) == 1
        assert preview.items[0].title == listing.title
        return True

    def sql(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.stack.app_url.replace("+psycopg", "")) as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall() if cursor.description else []
            conn.commit()
            return rows

    def required_products(self, report_id: str) -> set[str]:
        rows = self.sql(
            "SELECT s.product_ids FROM report_required_scopes r "
            "JOIN product_scope_snapshots s USING (scope_digest) "
            "WHERE r.report_id = %s",
            report_id,
        )
        assert len(rows) == 1
        return set(rows[0][0])


@pytest.fixture
def env(stack: Stack, tmp_path: Path) -> Iterator[ScopeEnv]:
    e = ScopeEnv(stack, tmp_path / "artifacts")
    yield e
    e.db.close()


async def test_widening_and_unrelated_narrowing_keep_access_required_does_not(
    env: ScopeEnv,
) -> None:
    alice, session = await env.executive(BASE)
    report_id, _ = await env.save(alice, session)
    assert env.required_products(report_id) == BASE
    assert await env.readable(alice, session, report_id)

    await env.entitle(alice, BASE | {"4", "5"})  # widened
    assert await env.readable(alice, session, report_id)

    await env.entitle(alice, BASE | {"5"})  # "4" was never required
    assert await env.readable(alice, session, report_id)

    await env.entitle(alice, {"1", "3", "4", "5"})  # "2" was required
    assert not await env.readable(alice, session, report_id)

    await env.entitle(alice, BASE)
    assert await env.readable(alice, session, report_id)


async def test_scope_in_result_rows_is_ignored(env: ScopeEnv) -> None:
    alice, session = await env.executive(BASE)
    report_id, _ = await env.save(alice, session, rows=ONE_PRODUCT_ROWS)
    assert env.required_products(report_id) == BASE
    await env.entitle(alice, {"1"})
    assert not await env.readable(alice, session, report_id)
    # Entitled to every product the rows mention, and more, but not 2 and 3.
    await env.entitle(alice, {"1", "101", "102", "103"})
    assert not await env.readable(alice, session, report_id)


async def test_scope_supplied_by_the_model_is_rejected_and_never_stored(
    env: ScopeEnv,
) -> None:
    alice, session = await env.executive(BASE)
    run = await env.run(alice, session)
    evidence_id = await env.evidence_with_rows(alice, run, ONE_PRODUCT_ROWS)
    principals = MemoryPrincipals()
    await principals.record(run, alice)
    registry = CapabilityRegistry(
        report_capabilities(env.reports, principals=principals, evidence=env.evidence)
    )
    ctx = await env.access.resolver.context_for_run(alice, run)
    arguments: dict[str, Any] = {
        "title": "September revenue by product",
        "summary": "Revenue by product for September 2026.",
        "findings": [{"text": "Product 1 earned 30.", "evidence_ids": [evidence_id]}],
    }
    for name in ("required_scope", "product_ids", "scope_digest", "product_scope"):
        result = await invoke(
            registry,
            ToolCall(
                call_id=_id("c"),
                name="save_report",
                arguments={**arguments, name: ["1"]},
            ),
            OperationContext(ctx, _id("op")),
            RecordingSink(),
        )
        assert isinstance(result.outcome, ToolFailed)
        assert result.outcome.code is ToolErrorCode.INVALID_INPUT
    assert await env.reports.list_reports(alice) == ()

    result = await invoke(
        registry,
        ToolCall(call_id=_id("c"), name="save_report", arguments=arguments),
        OperationContext(ctx, _id("op")),
        RecordingSink(),
    )
    assert isinstance(result.outcome, ToolSucceeded)
    report_id = result.outcome.output.report_id
    assert env.required_products(report_id) == BASE
    await env.entitle(alice, {"1"})
    assert not await env.readable(alice, session, report_id)


async def test_legacy_version_without_recorded_scope_keeps_the_strict_rule(
    env: ScopeEnv,
) -> None:
    alice, session = await env.executive(BASE)
    report_id, _ = await env.save(alice, session)
    # As if saved before T18-F1 with no recoverable set.
    env.sql("DELETE FROM report_required_scopes WHERE report_id = %s", report_id)
    await env.entitle(alice, BASE | {"4"})
    assert not await env.readable(alice, session, report_id)
    await env.entitle(alice, BASE)
    assert await env.readable(alice, session, report_id)


async def test_snapshots_are_immutable_and_digest_checked(env: ScopeEnv) -> None:
    alice, session = await env.executive(BASE)
    await env.save(alice, session)
    digest = product_set_digest(BASE)
    with psycopg.connect(env.stack.app_url.replace("+psycopg", "")) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "UPDATE product_scope_snapshots SET product_ids = %s "
                "WHERE scope_digest = %s",
                (["1", "2", "3", "4"], digest),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO product_scope_snapshots VALUES (%s, %s, now())",
                (product_set_digest({"9"}), ["1", "2", "3", "4"]),
            )
        conn.rollback()
