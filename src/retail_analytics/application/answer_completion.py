"""Whether a released answer completes its run: declined parts are resolved.

A request is complete when every part of it was either answered or declined
under a restriction the application enforces (with the alternative offered).
A correctly declined request, for example one customer's age band, is
therefore COMPLETED, not PARTIAL; a run stays PARTIAL while requested work
that the user may see is still unanswered, or when the answer rests on a cut
result.

The model's structured output says whether permitted work remains
(``complete``) and which restrictions it declined under (``declined``, a
closed set). A declined restriction counts only when the application
confirms it, never from the answer's wording:

- standing restrictions hold for every request whoever asks (customer
  demographics are group-level only; the compiler refuses the rest), so the
  application's own policy confirms them;
- access-dependent restrictions (outside the executive's permitted scope)
  are confirmed only by a refusal the application recorded in this run.

An unconfirmed restriction leaves the run PARTIAL: a model cannot reach
"complete" by declining work it was allowed to do. Budget, deadline and cost
stops never come here; they end PARTIAL through ``finish_partial``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from retail_analytics.application.contracts.investigations import Restriction
from retail_analytics.application.query_execution import (
    COMPILER_REJECTION_PREFIX,
    SCOPE_REFUSAL,
    is_compiler_rejection,
)
from retail_analytics.domain.executions import ToolExecution
from retail_analytics.domain.runs import RunStatus

# Enforced for every request, independent of the executive's access.
STANDING_RESTRICTIONS = frozenset({Restriction.INDIVIDUAL_DEMOGRAPHICS})

# Operation detail of a compiler refusal -> the restriction it applied.
_REFUSALS = {
    SCOPE_REFUSAL: Restriction.OUTSIDE_PERMITTED_SCOPE,
    f"{COMPILER_REJECTION_PREFIX}{Restriction.INDIVIDUAL_DEMOGRAPHICS.value}": (
        Restriction.INDIVIDUAL_DEMOGRAPHICS
    ),
}


def recorded_restrictions(
    operations: Iterable[ToolExecution],
) -> frozenset[Restriction]:
    """Restrictions the application applied in the run (refused queries)."""
    found = set()
    for op in operations:
        if is_compiler_rejection(op):
            restriction = _REFUSALS.get(op.error_detail or "")
            if restriction is not None:
                found.add(restriction)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class Completion:
    status: RunStatus
    # Declined restrictions the application confirmed / could not confirm.
    confirmed: tuple[Restriction, ...] = ()
    unconfirmed: tuple[Restriction, ...] = ()


def answer_completion(
    *,
    complete: bool,
    declined: Iterable[Restriction],
    recorded: frozenset[Restriction],
) -> Completion:
    """The run status for a released answer.

    ``complete``: the model found no permitted requested work unanswered.
    ``declined``: restrictions it declined parts of the request under.
    ``recorded``: restrictions the application applied in this run.
    (An answer citing a cut result is made PARTIAL by the caller.)
    """
    claimed = tuple(dict.fromkeys(declined))
    confirmed = tuple(r for r in claimed if r in STANDING_RESTRICTIONS or r in recorded)
    unconfirmed = tuple(r for r in claimed if r not in confirmed)
    resolved = complete and not unconfirmed
    return Completion(
        RunStatus.COMPLETED if resolved else RunStatus.PARTIAL,
        confirmed,
        unconfirmed,
    )


__all__ = [
    "STANDING_RESTRICTIONS",
    "Completion",
    "answer_completion",
    "recorded_restrictions",
]
