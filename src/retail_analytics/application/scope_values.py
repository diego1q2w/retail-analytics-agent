"""Requested values outside the executive's permitted scope (brands).

The SQL compiler binds every query to the executive's effective product
scope (explicit grants, assigned brands, administrator grants), so a query
about a brand outside it reads no rows. An empty or zero result is then
*not* a business result: the true figure is unknown to this user. This
module recognizes that case from trusted metadata before the query runs:

- the compiler reports literal comparisons on string fields
  (``CompiledQuery.value_filters``);
- the permitted values are the brands of the products in the effective
  scope, from the synced brand snapshot (never from model or user text);
- a brand matching none of them is *outside the permitted scope*. Whether it
  exists elsewhere is neither known nor revealed: a brand held by another
  manager and a name that exists nowhere get the same wording;
- a query whose ``WHERE`` requires only such brands is refused before any
  warehouse job (it could only produce a misleading zero); a query that also
  covers permitted brands runs, with a note naming the outside ones;
- for a name that matches nothing, close matches among the *permitted*
  brands are offered (the bounded permitted-catalog lookup).

A permitted brand with no sales is unaffected: its query runs and a genuine
zero may be reported. When no brand snapshot covers the scope (not synced),
nothing is said and queries run as before. Only brands have a trusted
snapshot; other fields (category, product name) are not judged here.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from retail_analytics.application.contracts.query_compiler import (
    FieldRef,
    ValueFilter,
)
from retail_analytics.application.ports.scope_values import PermittedBrands
from retail_analytics.domain.access import ProductScope

BRAND = FieldRef("products", "brand")
# Durable reason (operation error detail) of a refused query.
OUTSIDE_SCOPE = "outside_permitted_scope"
_MAX_SHOWN = 5
_MAX_CHARS = 60
_SIMILAR_CUTOFF = 0.75
_UNSAFE = re.compile(r"[^\w .,&'+/-]")

# The rule the model sees; tool messages below repeat its key words.
NO_FIGURE_RULE = (
    "There is no figure for it for this user: never 0, never 'no sales'. "
    "Do not query, describe or look it up again. Say plainly, without jokes "
    "or humour, that it is outside their permitted scope, and do not say "
    "whether it exists elsewhere. That statement answers the request "
    "completely (complete: true)."
)
REFUSED_MESSAGE = (
    "Outside the user's permitted scope: the query was not run. " + NO_FIGURE_RULE
)


@dataclass(frozen=True, slots=True)
class ScopeAssessment:
    """Requested brands outside the permitted scope, and what to do."""

    outside: tuple[str, ...]
    # Some required filter matches no permitted brand: do not run the query.
    refuse: bool
    # Close names among the permitted brands (for names matching nothing).
    similar: tuple[str, ...] = ()

    def refusal(self) -> str:
        """Model-facing message for a refused query."""
        return (
            f"Outside the user's permitted scope: no permitted product has "
            f"brand {_listed(self.outside)}; the query was not run. "
            + NO_FIGURE_RULE
            + self._similar_text()
        )

    def note(self) -> str:
        """Model-facing note for a query that ran over permitted brands too."""
        return (
            f"Brand {_listed(self.outside)} is outside the user's permitted "
            "scope: this result contains nothing about it. "
            + NO_FIGURE_RULE
            + self._similar_text()
        )

    def _similar_text(self) -> str:
        if not self.similar:
            return ""
        return (
            " Permitted brands with similar names (ask the user if one was "
            f"meant): {_listed(self.similar)}."
        )


class ScopeValueCheck:
    def __init__(self, brands: PermittedBrands) -> None:
        self._brands = brands

    async def assess(
        self, filters: Sequence[ValueFilter], scope: ProductScope
    ) -> ScopeAssessment | None:
        """None when nothing requested is outside (or nothing is known)."""
        relevant = [f for f in filters if f.field == BRAND and f.values]
        if not relevant or scope.is_empty:
            return None
        permitted = await self._brands.brands_within(scope.product_ids)
        if not permitted:
            return None  # no snapshot for this scope: nothing can be judged
        folded = {b.casefold().strip(): b for b in permitted}
        outside: list[str] = []
        refuse = False
        for item in relevant:
            missing = [
                v for v in item.values if not _matches(v, folded, pattern=item.pattern)
            ]
            outside.extend(m for m in missing if m not in outside)
            if item.required and len(missing) == len(item.values):
                refuse = True
        if not outside:
            return None
        return ScopeAssessment(
            outside=tuple(_safe(v) for v in outside[:_MAX_SHOWN]),
            refuse=refuse,
            similar=_similar(
                (v for v in outside if "%" not in v and "_" not in v), folded
            ),
        )


def _matches(value: str, folded: dict[str, str], *, pattern: bool) -> bool:
    if not pattern:
        return value.casefold().strip() in folded
    regex = re.compile(
        "".join(
            ".*" if c == "%" else "." if c == "_" else re.escape(c)
            for c in value.casefold().strip()
        )
    )
    return any(regex.fullmatch(name) for name in folded)


def _similar(values: Iterable[str], folded: dict[str, str]) -> tuple[str, ...]:
    found: list[str] = []
    for value in values:
        for match in difflib.get_close_matches(
            value.casefold().strip(), list(folded), n=3, cutoff=_SIMILAR_CUTOFF
        ):
            if folded[match] not in found:
                found.append(folded[match])
    return tuple(_safe(v) for v in found[:_MAX_SHOWN])


def _safe(value: str) -> str:
    """Quoted request text: bounded, no markup or control characters."""
    return _UNSAFE.sub("", value)[:_MAX_CHARS]


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)
