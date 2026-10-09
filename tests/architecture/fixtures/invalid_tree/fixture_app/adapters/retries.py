"""A non-neutral adapter helper that loads the Temporal SDK."""

from temporalio.common import RetryPolicy

POLICY = RetryPolicy(maximum_attempts=3)
