"""A process running local investigations, to be killed by the restart tests.

It opens the local manager over the held-out fixture warehouse, starts an
investigation whose warehouse job never finishes, queues a request behind it
and opens a clarification in a second session, prints their identifiers and
then waits to be killed. No Temporal is imported or contacted.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from retail_analytics.adapters.evaluation.fixture_warehouse import FixtureWarehouse
from retail_analytics.adapters.models.scripted import scripted_model
from retail_analytics.application.contracts.evaluation import (
    ScenarioInput,
    ScopeSpec,
    Turn,
)
from retail_analytics.application.contracts.warehouse_jobs import (
    JobRef,
    JobSnapshot,
    JobState,
    JobSubmission,
)
from retail_analytics.bootstrap.agent_evaluation import (
    AgentRuntimeTarget,
    FixtureSource,
    heldout_source,
)
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.domain.runs import ExecutionBackend

ROOT = Path(__file__).resolve().parents[2]
SCOPE = ("201", "202", "203", "204", "205", "206", "207", "208")
SEPT = "ordered_date >= DATE '2026-09-01' AND ordered_date < DATE '2026-10-01'"
REVENUE_SQL = (
    "SELECT SUM(sale_amount) AS revenue FROM sales_items "  # noqa: S608
    f"WHERE item_status = 'Complete' AND {SEPT}"
)
HELD = "Hold the September 2026 revenue query."
QUEUED = "Afterwards, list September 2026 revenue by product."
ASK = "Analyze the outage period for me."
FRESH = "What was our revenue in September 2026?"
PLANS: dict[str, list[dict[str, Any]]] = {
    HELD: [
        {"call": "execute_analysis", "args": {"sql": REVENUE_SQL, "purpose": "t"}},
        {"answer": {"text": "Held {{value:revenue}}.", "cite": ["revenue"]}},
    ],
    QUEUED: [{"answer": {"text": "The queued request ran.", "cite": []}}],
    ASK: [{"ask": "Which months should I compare?"}],
    FRESH: [
        {"call": "execute_analysis", "args": {"sql": REVENUE_SQL, "purpose": "t"}},
        {
            "answer": {
                "text": "{{value:revenue}} [{{evidence:revenue}}].",
                "cite": ["revenue"],
            }
        },
    ],
}


class HeldWarehouse(FixtureWarehouse):
    """The fixture warehouse whose jobs keep running until cancelled (when
    ``hold``); records every cancellation it receives."""

    def __init__(self, base: FixtureWarehouse, *, hold: bool) -> None:
        super().__init__(base._db, base.data_ref)
        self.hold = hold
        self.cancelled: list[str] = []
        self._running: set[str] = set()

    async def submit(self, submission: JobSubmission) -> JobSnapshot:
        snapshot = await super().submit(submission)
        if not self.hold:
            return snapshot
        self._running.add(submission.ref.job_id)
        return replace(snapshot, state=JobState.RUNNING)

    async def lookup(self, ref: JobRef) -> JobSnapshot | None:
        snapshot = await super().lookup(ref)
        if snapshot is not None and ref.job_id in self._running:
            return replace(snapshot, state=JobState.RUNNING)
        return snapshot

    async def cancel(self, ref: JobRef) -> None:
        self.cancelled.append(ref.job_id)
        self._running.discard(ref.job_id)


def held_source(*, hold: bool) -> FixtureSource:
    source = heldout_source(ROOT / "evaluation")
    return replace(source, warehouse=HeldWarehouse(source.warehouse, hold=hold))


def case(scenario_id: str, text: str) -> ScenarioInput:
    return ScenarioInput(
        scenario_id=scenario_id,
        mode="fixture",
        fixture_ref="heldout-fixture-1",
        scope=ScopeSpec(executive_ref="local-exec", product_scope=SCOPE),
        dialogue=(Turn(text=text),),
    )


def target(settings: BackendSettings, *, hold: bool) -> AgentRuntimeTarget:
    return AgentRuntimeTarget(
        settings,
        held_source(hold=hold),
        scripted_model(PLANS),
        seed_knowledge=False,
        backend=ExecutionBackend.LOCAL,
    )


async def main() -> None:
    settings = BackendSettings(
        database_url=SecretStr(os.environ["LOCAL_DATABASE_URL"]),
        artifact_dir=Path(os.environ["LOCAL_ARTIFACT_DIR"]),
    )
    local = target(settings, hold=True)
    harness = await local._start()
    control = harness.services.control
    held_case = case(os.environ["LOCAL_SCENARIO"], HELD)
    principal, session_id = await local.provision(
        held_case, local.source.products(SCOPE)
    )
    held = await control.start(
        principal, session_id=session_id, text=HELD, submission_key=uuid.uuid4().hex
    )
    queued = await control.enqueue(
        principal, session_id=session_id, text=QUEUED, submission_key="queued"
    )
    asking, ask_session = await local.provision(
        case(os.environ["LOCAL_SCENARIO"] + "-ask", ASK), local.source.products(SCOPE)
    )
    waiting = await control.start(
        asking, session_id=ask_session, text=ASK, submission_key=uuid.uuid4().hex
    )
    print(
        json.dumps(
            {
                "executive": principal.executive_id,
                "session": session_id,
                "held": held.run_id,
                "queued": queued.input_id,
                "waiting": waiting.run_id,
                "instance": None
                if harness.manager is None
                else harness.manager.instance_id,
            }
        ),
        flush=True,
    )
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
