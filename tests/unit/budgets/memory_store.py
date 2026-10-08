"""In-memory ``RunBudgetStore`` with the PostgreSQL adapter's contract.

One lock per store stands in for the run-row lock; every operation yields to
the event loop inside it, so concurrent callers really interleave around it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from retail_analytics.application.persistence import RecordNotFound
from retail_analytics.domain.budgets import Charge, ChargeKind, RunBudget, RunLimits


class MemoryRunBudgetStore:
    def __init__(self) -> None:
        self.budgets: dict[str, RunBudget] = {}
        self.charge_rows: dict[tuple[str, ChargeKind, str], Charge] = {}
        self._lock = asyncio.Lock()

    def _load(
        self, run_id: str, limits: RunLimits | None = None, at: datetime | None = None
    ) -> RunBudget:
        budget = self.budgets.get(run_id)
        if budget is None:
            if limits is None or at is None:
                raise RecordNotFound("run budget", run_id)
            budget = RunBudget.open(run_id, limits, at=at)
            self.budgets[run_id] = budget
        return budget

    async def open(self, run_id: str, limits: RunLimits, *, at: datetime) -> RunBudget:
        async with self._lock:
            await asyncio.sleep(0)
            return self._load(run_id, limits, at)

    async def get(self, run_id: str) -> RunBudget | None:
        return self.budgets.get(run_id)

    async def pause(self, run_id: str, *, at: datetime) -> RunBudget:
        async with self._lock:
            budget = self._load(run_id).pause(at=at)
            self.budgets[run_id] = budget
            return budget

    async def resume(self, run_id: str, *, at: datetime) -> RunBudget:
        async with self._lock:
            budget = self._load(run_id).resume(at=at)
            self.budgets[run_id] = budget
            return budget

    async def charge_query(
        self,
        run_id: str,
        key: str,
        estimated_bytes: int,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        async with self._lock:
            budget = self._load(run_id, limits, at)
            await asyncio.sleep(0)
            existing = self.charge_rows.get((run_id, ChargeKind.QUERY, key))
            if existing is not None:
                return existing
            budget, charge = budget.charge_query(key, estimated_bytes, at=at)
            self.budgets[run_id] = budget
            self.charge_rows[(run_id, charge.kind, key)] = charge
            return charge

    async def settle_query(
        self, run_id: str, key: str, actual_bytes: int | None
    ) -> Charge | None:
        return await self._settle(run_id, ChargeKind.QUERY, key, actual_bytes)

    async def charge_provider_request(
        self,
        run_id: str,
        key: str,
        estimated_tokens: int,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        async with self._lock:
            budget = self._load(run_id, limits, at)
            await asyncio.sleep(0)
            existing = self.charge_rows.get((run_id, ChargeKind.PROVIDER_REQUEST, key))
            if existing is not None:
                return existing
            budget, charge = budget.charge_provider_request(
                key, estimated_tokens, at=at
            )
            self.budgets[run_id] = budget
            self.charge_rows[(run_id, charge.kind, key)] = charge
            return charge

    async def settle_provider_request(
        self, run_id: str, key: str, reported_tokens: int | None
    ) -> Charge | None:
        return await self._settle(
            run_id, ChargeKind.PROVIDER_REQUEST, key, reported_tokens
        )

    async def _settle(
        self, run_id: str, kind: ChargeKind, key: str, amount: int | None
    ) -> Charge | None:
        async with self._lock:
            budget = self.budgets.get(run_id)
            charge = self.charge_rows.get((run_id, kind, key))
            if budget is None or charge is None or charge.settled:
                return charge
            if kind is ChargeKind.QUERY:
                budget, charge = budget.settle_query(charge, amount)
            else:
                budget, charge = budget.settle_provider_request(charge, amount)
            self.budgets[run_id] = budget
            self.charge_rows[(run_id, kind, key)] = charge
            return charge

    async def charge_correction(
        self,
        run_id: str,
        key: str,
        corrects: str,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        async with self._lock:
            budget = self._load(run_id, limits, at)
            await asyncio.sleep(0)
            kind = ChargeKind.CORRECTION
            existing = self.charge_rows.get((run_id, kind, key))
            if existing is not None:
                return existing
            parent = self.charge_rows.get((run_id, kind, corrects))
            root = corrects if parent is None or parent.group is None else parent.group
            used = sum(
                1
                for (run, k, _), c in self.charge_rows.items()
                if run == run_id and k is kind and c.group == root
            )
            charge = replace(
                budget.charge_correction(key, root, used, at=at), settled=True
            )
            self.charge_rows[(run_id, kind, key)] = charge
            return charge

    async def charges(self, run_id: str) -> Sequence[Charge]:
        return [c for (run, _, _), c in self.charge_rows.items() if run == run_id]
