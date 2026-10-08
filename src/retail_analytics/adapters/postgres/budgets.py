"""PostgreSQL ``RunBudgetStore``.

Every operation is one transaction that first locks the run's ``run_budgets``
row (creating it with the given limits if absent), so concurrent charges of
one run, from any number of workers, are serialized: each sees the previous
one's usage and none can jointly exceed a limit. A charge already recorded
under the same (run, kind, key) is returned as is, so retried activities
charge once. The domain ``RunBudget`` decides; this adapter only persists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import budget_charges, run_budgets
from retail_analytics.application.persistence import RecordNotFound
from retail_analytics.domain.budgets import (
    Charge,
    ChargeKind,
    RunBudget,
    RunLimits,
    RunUsage,
)


def _budget(row: sa.Row[tuple[object, ...]]) -> RunBudget:
    m = row._mapping
    return RunBudget(
        run_id=m["run_id"],
        limits=RunLimits.from_dict(m["limits"]),
        usage=RunUsage(
            active_seconds_used=float(m["active_seconds_used"]),
            active_since=m["active_since"],
            provider_requests=m["provider_requests"],
            tokens=m["tokens"],
            queries=m["queries"],
            bytes=m["bytes"],
        ),
    )


def _charge(row: sa.Row[tuple[object, ...]]) -> Charge:
    m = row._mapping
    return Charge(
        kind=ChargeKind(m["kind"]),
        key=m["charge_key"],
        group=m["group_key"],
        bytes=m["bytes"],
        tokens=m["tokens"],
        settled=m["settled"],
        ambiguous=m["ambiguous"],
    )


class PostgresRunBudgetStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- row locking and persistence ----------------------------------------

    def _lock(
        self,
        connection: sa.Connection,
        run_id: str,
        limits: RunLimits | None = None,
        at: datetime | None = None,
    ) -> RunBudget:
        if limits is not None:
            opened = RunBudget.open(run_id, limits, at=at or self._db.clock())
            now = self._db.clock()
            connection.execute(
                insert(run_budgets)
                .values(
                    run_id=run_id,
                    limits=limits.as_dict(),
                    active_seconds_used=0.0,
                    active_since=opened.usage.active_since,
                    provider_requests=0,
                    tokens=0,
                    queries=0,
                    bytes=0,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["run_id"])
            )
        row = connection.execute(
            sa.select(run_budgets)
            .where(run_budgets.c.run_id == run_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise RecordNotFound("run budget", run_id)
        return _budget(row)

    def _save(self, connection: sa.Connection, budget: RunBudget) -> None:
        usage = budget.usage
        connection.execute(
            sa.update(run_budgets)
            .where(run_budgets.c.run_id == budget.run_id)
            .values(
                active_seconds_used=usage.active_seconds_used,
                active_since=usage.active_since,
                provider_requests=usage.provider_requests,
                tokens=usage.tokens,
                queries=usage.queries,
                bytes=usage.bytes,
                updated_at=self._db.clock(),
            )
        )

    @staticmethod
    def _find(
        connection: sa.Connection, run_id: str, kind: ChargeKind, key: str
    ) -> Charge | None:
        row = connection.execute(
            sa.select(budget_charges).where(
                budget_charges.c.run_id == run_id,
                budget_charges.c.kind == kind.value,
                budget_charges.c.charge_key == key,
            )
        ).one_or_none()
        return None if row is None else _charge(row)

    def _insert(self, connection: sa.Connection, run_id: str, charge: Charge) -> None:
        connection.execute(
            sa.insert(budget_charges).values(
                run_id=run_id,
                kind=charge.kind.value,
                charge_key=charge.key,
                group_key=charge.group,
                bytes=charge.bytes,
                tokens=charge.tokens,
                settled=charge.settled,
                ambiguous=charge.ambiguous,
                created_at=self._db.clock(),
            )
        )

    def _update_charge(
        self, connection: sa.Connection, run_id: str, charge: Charge
    ) -> None:
        connection.execute(
            sa.update(budget_charges)
            .where(
                budget_charges.c.run_id == run_id,
                budget_charges.c.kind == charge.kind.value,
                budget_charges.c.charge_key == charge.key,
            )
            .values(
                bytes=charge.bytes,
                tokens=charge.tokens,
                settled=charge.settled,
                ambiguous=charge.ambiguous,
                settled_at=self._db.clock(),
            )
        )

    # -- port ----------------------------------------------------------------

    async def open(self, run_id: str, limits: RunLimits, *, at: datetime) -> RunBudget:
        return await self._db.transaction(self._lock, run_id, limits, at)

    async def get(self, run_id: str) -> RunBudget | None:
        return await self._db.transaction(self._get, run_id)

    @staticmethod
    def _get(connection: sa.Connection, run_id: str) -> RunBudget | None:
        row = connection.execute(
            sa.select(run_budgets).where(run_budgets.c.run_id == run_id)
        ).one_or_none()
        return None if row is None else _budget(row)

    async def pause(self, run_id: str, *, at: datetime) -> RunBudget:
        return await self._db.transaction(self._clock_change, run_id, at, False)

    async def resume(self, run_id: str, *, at: datetime) -> RunBudget:
        return await self._db.transaction(self._clock_change, run_id, at, True)

    def _clock_change(
        self, connection: sa.Connection, run_id: str, at: datetime, running: bool
    ) -> RunBudget:
        budget = self._lock(connection, run_id)
        updated = budget.resume(at=at) if running else budget.pause(at=at)
        if updated != budget:
            self._save(connection, updated)
        return updated

    async def charge_query(
        self,
        run_id: str,
        key: str,
        estimated_bytes: int,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        return await self._db.transaction(
            self._charge_query, run_id, key, estimated_bytes, limits, at
        )

    def _charge_query(
        self,
        connection: sa.Connection,
        run_id: str,
        key: str,
        estimated_bytes: int,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        budget = self._lock(connection, run_id, limits, at)
        existing = self._find(connection, run_id, ChargeKind.QUERY, key)
        if existing is not None:
            return existing
        updated, charge = budget.charge_query(key, estimated_bytes, at=at)
        self._save(connection, updated)
        self._insert(connection, run_id, charge)
        return charge

    async def settle_query(
        self, run_id: str, key: str, actual_bytes: int | None
    ) -> Charge | None:
        return await self._db.transaction(
            self._settle, run_id, ChargeKind.QUERY, key, actual_bytes
        )

    async def charge_provider_request(
        self,
        run_id: str,
        key: str,
        estimated_tokens: int,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        return await self._db.transaction(
            self._charge_provider, run_id, key, estimated_tokens, limits, at
        )

    def _charge_provider(
        self,
        connection: sa.Connection,
        run_id: str,
        key: str,
        estimated_tokens: int,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        budget = self._lock(connection, run_id, limits, at)
        existing = self._find(connection, run_id, ChargeKind.PROVIDER_REQUEST, key)
        if existing is not None:
            return existing
        updated, charge = budget.charge_provider_request(key, estimated_tokens, at=at)
        self._save(connection, updated)
        self._insert(connection, run_id, charge)
        return charge

    async def settle_provider_request(
        self, run_id: str, key: str, reported_tokens: int | None
    ) -> Charge | None:
        return await self._db.transaction(
            self._settle, run_id, ChargeKind.PROVIDER_REQUEST, key, reported_tokens
        )

    def _settle(
        self,
        connection: sa.Connection,
        run_id: str,
        kind: ChargeKind,
        key: str,
        amount: int | None,
    ) -> Charge | None:
        try:
            budget = self._lock(connection, run_id)
        except RecordNotFound:
            return None
        charge = self._find(connection, run_id, kind, key)
        if charge is None or charge.settled:
            return charge
        if kind is ChargeKind.QUERY:
            updated, settled = budget.settle_query(charge, amount)
        else:
            updated, settled = budget.settle_provider_request(charge, amount)
        if updated != budget:
            self._save(connection, updated)
        self._update_charge(connection, run_id, settled)
        return settled

    async def charge_correction(
        self,
        run_id: str,
        key: str,
        corrects: str,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        return await self._db.transaction(
            self._charge_correction, run_id, key, corrects, limits, at
        )

    def _charge_correction(
        self,
        connection: sa.Connection,
        run_id: str,
        key: str,
        corrects: str,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        budget = self._lock(connection, run_id, limits, at)
        existing = self._find(connection, run_id, ChargeKind.CORRECTION, key)
        if existing is not None:
            return existing
        parent = self._find(connection, run_id, ChargeKind.CORRECTION, corrects)
        root = corrects if parent is None or parent.group is None else parent.group
        used = connection.execute(
            sa.select(sa.func.count())
            .select_from(budget_charges)
            .where(
                budget_charges.c.run_id == run_id,
                budget_charges.c.kind == ChargeKind.CORRECTION.value,
                budget_charges.c.group_key == root,
            )
        ).scalar_one()
        charge = budget.charge_correction(key, root, int(used), at=at)
        self._insert(connection, run_id, replace(charge, settled=True))
        return replace(charge, settled=True)

    async def charges(self, run_id: str) -> Sequence[Charge]:
        return await self._db.transaction(self._charges, run_id)

    @staticmethod
    def _charges(connection: sa.Connection, run_id: str) -> list[Charge]:
        rows = connection.execute(
            sa.select(budget_charges)
            .where(budget_charges.c.run_id == run_id)
            .order_by(budget_charges.c.created_at, budget_charges.c.charge_key)
        ).all()
        return [_charge(row) for row in rows]
