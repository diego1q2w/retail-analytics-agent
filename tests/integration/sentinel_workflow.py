"""Minimal workflow for stack checks; kept import-light for Temporal's sandbox."""

from temporalio import workflow


@workflow.defn
class SentinelWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"hello {name}"
