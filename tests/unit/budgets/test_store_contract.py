"""The in-memory budget store obeys the shared store contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

import pytest

from retail_analytics.application.budgets import RunBudgetStore
from tests.unit.budgets.contract import CONTRACT
from tests.unit.budgets.memory_store import MemoryRunBudgetStore


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CONTRACT, ids=lambda case: case.__name__)
async def test_memory_store_contract(
    case: Callable[[Sequence[RunBudgetStore]], Awaitable[None]],
) -> None:
    await case([MemoryRunBudgetStore()])
