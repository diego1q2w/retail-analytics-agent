"""Idempotent Temporal scheduling; inputs and signals contain only run IDs."""

from __future__ import annotations

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from retail_analytics.adapters.temporal.workflow import InvestigationWorkflow
from retail_analytics.domain.runs import ExecutionBackend, WorkflowRef


class TemporalInvestigationScheduler:
    def __init__(self, client: Client, task_queue: str) -> None:
        self._client = client
        self._task_queue = task_queue

    @property
    def backend(self) -> ExecutionBackend:
        return ExecutionBackend.TEMPORAL

    @staticmethod
    def workflow_id(run_id: str) -> str:
        return "investigation/" + run_id

    async def start(self, run_id: str) -> WorkflowRef:
        workflow_id = self.workflow_id(run_id)
        try:
            handle = await self._client.start_workflow(
                InvestigationWorkflow.run,
                run_id,
                id=workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            return WorkflowRef(workflow_id, handle.first_execution_run_id)
        except WorkflowAlreadyStartedError as started:
            return WorkflowRef(workflow_id, started.run_id)

    async def notify_input(self, run_id: str) -> None:
        await self._client.get_workflow_handle(self.workflow_id(run_id)).signal(
            InvestigationWorkflow.input_available
        )

    async def request_cancel(self, run_id: str) -> None:
        await self._client.get_workflow_handle(self.workflow_id(run_id)).signal(
            InvestigationWorkflow.cancel_requested
        )
