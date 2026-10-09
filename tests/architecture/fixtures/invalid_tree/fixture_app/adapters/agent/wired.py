"""Invalid: shared agent code importing the project's Temporal adapter."""

from fixture_app.adapters.temporal import worker

WORKER = worker.QUEUE
