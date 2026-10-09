"""Temporal assembly: client, scheduler and the investigation worker.

The only composition module that knows the investigations run on Temporal,
used only with ``RETAIL_ANALYTICS_EXECUTION_BACKEND=temporal`` (opt-in).
General construction (``bootstrap.investigations``) builds runtime-neutral
services; the worker, API and evaluation roots take them here to connect,
schedule, bind and register.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from retail_analytics.adapters.temporal.activities import REGISTERED, bind_runtime
from retail_analytics.adapters.temporal.agent import bind_agent_services
from retail_analytics.adapters.temporal.scheduler import TemporalInvestigationScheduler
from retail_analytics.adapters.temporal.workflow import InvestigationWorkflow
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.investigations import InvestigationServices


async def connect(address: str, namespace: str, *, lazy: bool = False) -> Client:
    """A client that can carry Pydantic AI payloads (lazy: connect on first use)."""
    return await Client.connect(
        address, namespace=namespace, plugins=[PydanticAIPlugin()], lazy=lazy
    )


def scheduler(client: Client, task_queue: str) -> TemporalInvestigationScheduler:
    return TemporalInvestigationScheduler(client, task_queue)


@asynccontextmanager
async def api_scheduler(
    settings: BackendSettings,
) -> AsyncIterator[TemporalInvestigationScheduler]:
    """The API's scheduler: starts workflows for ``retail-analytics-worker``.

    The client connects lazily, so the API can start before Temporal.
    """
    client = await connect(
        settings.temporal_address or "", settings.temporal_namespace, lazy=True
    )
    yield scheduler(client, settings.temporal_task_queue)


def bind_worker(services: InvestigationServices) -> None:
    """Give this process's workflow activities and durable agent their services.

    Call once, before a worker starts polling; the Temporal adapter keeps them
    in its registration objects because Temporal registers activities at
    import time.
    """
    bind_runtime(services.runtime)
    bind_agent_services(services.agent)


def investigation_worker(
    client: Client, task_queue: str, services: InvestigationServices
) -> Worker:
    """A worker for the investigation workflow and its activities, bound to
    ``services``."""
    bind_worker(services)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[InvestigationWorkflow],
        activities=REGISTERED,
    )
