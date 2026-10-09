"""Controlled worker/provider used by process-termination recovery tests."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from pydantic import SecretStr
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.models.function import FunctionModel
from temporalio.client import Client

from retail_analytics.adapters.temporal.scheduler import TemporalInvestigationScheduler
from retail_analytics.bootstrap.access import build_access, local_token_authority
from retail_analytics.bootstrap.config import BackendSettings, RuntimeMode
from retail_analytics.bootstrap.investigations import build_investigations
from retail_analytics.bootstrap.persistence import build_persistence
from retail_analytics.bootstrap.telemetry import install_from_settings
from retail_analytics.bootstrap.temporal import investigation_worker
from tests.integration.scripted_investigations import (
    EffectInput,
    EffectOutput,
    effect_registry,
    fallback_chain,
    scripted_model,
)

__all__ = [
    "EffectInput",
    "EffectOutput",
    "effect_registry",
    "fallback_chain",
    "scripted_model",
]


async def main() -> None:
    settings = BackendSettings(
        mode=RuntimeMode.FIXTURE,
        database_url=SecretStr(os.environ["T13_DATABASE_URL"]),
        temporal_address=os.environ["T13_TEMPORAL_ADDRESS"],
        temporal_task_queue=os.environ["T13_TASK_QUEUE"],
        auth_signing_key=SecretStr("test-key-" + "x" * 32),
        run_max_provider_requests=int(
            os.environ.get("T39_MAX_PROVIDER_REQUESTS", "20")
        ),
    )
    if os.environ.get("T11_ACTIVE_SECONDS"):
        # Short test limits bypass the operator minimum on purpose.
        settings = settings.model_copy(
            update={"run_active_seconds": int(os.environ["T11_ACTIVE_SECONDS"])}
        )
    assert settings.database_url is not None
    if os.environ.get("T30_TRACES_ENDPOINT"):
        install_from_settings(
            settings.model_copy(
                update={
                    "telemetry_enabled": True,
                    "telemetry_traces_endpoint": os.environ["T30_TRACES_ENDPOINT"],
                    "telemetry_metrics_endpoint": os.environ["T30_METRICS_ENDPOINT"],
                    "telemetry_export_timeout_seconds": 1.0,
                    "telemetry_metric_interval_seconds": 1.0,
                }
            ),
            "worker",
        )
    db = build_persistence(settings.database_url.get_secret_value())
    client = await Client.connect(
        settings.temporal_address or "", plugins=[PydanticAIPlugin()]
    )
    scheduler = TemporalInvestigationScheduler(client, settings.temporal_task_queue)
    access = build_access(db, local_token_authority(settings))
    model: Any = FunctionModel(scripted_model, model_name="scripted")
    if os.environ.get("T14_PROVIDERS") == "fallback":
        model = fallback_chain(settings)
    services = build_investigations(
        settings,
        db,
        access,
        scheduler,
        model,
        registry=effect_registry(db),
    )
    try:
        async with investigation_worker(client, settings.temporal_task_queue, services):
            print("WORKER_READY", flush=True)
            await asyncio.Event().wait()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
