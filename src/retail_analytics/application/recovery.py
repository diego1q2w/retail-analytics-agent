"""What the runtime does next with a query outcome (design sections 17/30).

One classification shared by the workflow and the model-facing tool result,
so retries, reformulations, empty results and budget stops are handled the
same way everywhere:

- ``WAIT``/``RECONCILE``: a job may exist; check the recorded job (or call
  ``reconcile_cancel`` for a stopping one) and never submit a new operation;
- ``RETRY``: same operation, next attempt, after ``RunBudgets.retry_decision``
  allows it (attempts and time remain; backoff delay);
- ``REFORMULATE``: the model may correct the query as a new operation after
  ``RunBudgets.reserve_correction``;
- ``NARROW``: a per-query limit (bytes, deadline) was hit; a narrower query
  may succeed, also charged as a correction;
- ``EMPTY``: a valid, complete, empty result: inspect assumptions or ask; do
  not silently broaden the question;
- ``STOP``: run budget exhausted, access denied or an unrecoverable error;
  keep completed evidence and report partial findings.

``complete`` is False whenever rows were withheld or cut, so a truncated or
partial result is never presented as the whole answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from retail_analytics.application.query_execution import (
    QUERY_DEADLINE,
    QueryCancelled,
    QueryFailed,
    QueryOutcome,
    QueryOutcomeUnknown,
    QueryPending,
    QuerySucceeded,
)
from retail_analytics.domain.operations import ToolErrorCode


class RecoveryAction(StrEnum):
    DONE = "done"
    EMPTY = "empty"
    WAIT = "wait"
    RECONCILE = "reconcile"
    RETRY = "retry"
    REFORMULATE = "reformulate"
    NARROW = "narrow"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class Recovery:
    action: RecoveryAction
    # True only for a successful result holding every row of the answer.
    complete: bool = False
    # Rows were cut by the row/byte caps or the executor's read limit.
    truncated: bool = False


_PER_QUERY_BUDGET = (
    "estimate_over_query_limit",
    "query_budget_",
    QUERY_DEADLINE,
    "job_bytesBilledLimitExceeded",
    "job_resourcesExceeded",
    "job_responseTooLarge",
    "job_timeout",
)


def classify(outcome: QueryOutcome) -> Recovery:
    if isinstance(outcome, QuerySucceeded):
        result = outcome.result
        if result.truncated:
            return Recovery(RecoveryAction.DONE, complete=False, truncated=True)
        if not result.rows:
            return Recovery(RecoveryAction.EMPTY, complete=True)
        return Recovery(RecoveryAction.DONE, complete=True)
    if isinstance(outcome, QueryPending):
        return Recovery(RecoveryAction.WAIT)
    if isinstance(outcome, QueryOutcomeUnknown):
        return Recovery(RecoveryAction.RECONCILE)
    if isinstance(outcome, QueryCancelled):
        if outcome.confirmed:
            return Recovery(RecoveryAction.STOP)
        return Recovery(RecoveryAction.RECONCILE)
    return _failure(outcome)


def _failure(failed: QueryFailed) -> Recovery:
    if failed.stopping is not None:
        return Recovery(RecoveryAction.RECONCILE)
    if failed.retryable:
        return Recovery(RecoveryAction.RETRY)
    if failed.code is ToolErrorCode.BUDGET_EXCEEDED:
        if failed.reason.startswith(_PER_QUERY_BUDGET):
            return Recovery(RecoveryAction.NARROW)
        return Recovery(RecoveryAction.STOP)
    if failed.correctable:
        return Recovery(RecoveryAction.REFORMULATE)
    return Recovery(RecoveryAction.STOP)
