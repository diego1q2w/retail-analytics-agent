"""Invalid: shared agent code importing the Temporal SDK directly."""

from temporalio import workflow

IN_WORKFLOW = workflow.in_workflow
