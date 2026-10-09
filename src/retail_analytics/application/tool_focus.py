"""Which authorized tools one model request sees: focused, and expandable.

The permission-filtered catalog (``CapabilityRegistry.catalog``) is what an
executive *may* use. Most requests need a small part of it: ordinary analysis
needs schema, query, evidence and reviewed examples, not report management,
preference changes, deletion or currency conversion. Sending every schema on
every request costs input tokens and invites setup calls the request does not
need, so a few self-contained groups are exposed on demand:

- a group's tools are exposed when the request (including steering that
  arrived since) mentions what the group is for, when an effective preference
  needs it, or after the model called the group's loader earlier in the run;
- otherwise only the group's loader is exposed: a tiny argument-free tool the
  model calls when the investigation turns out to need the group, so mixed
  and follow-up requests broaden within the same run, in any order;
- every other authorized tool is always exposed.

Request hints are relevance, not security: they never add a tool the catalog
lacks (selection only intersects the authorized set), are re-evaluated on every
step and never remove a group the run already loaded. Execution keeps its own
authorization check (``registry.resolve``) for every call. The selection is a
pure function of the authorized tool names, the request and preference text
and the run's recorded loader operations, so it is recomputed identically by
the local and Temporal runtimes, inside the step activity.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from retail_analytics.application.contracts.investigations import (
    FocusReason,
    ToolFocus,
)
from retail_analytics.application.investigation_policy import (
    CONFIRM_PREFERENCE,
    CONVERT_CURRENCY,
    DECLINE_PREFERENCE,
    EXPORT_REPORT,
    FORGET_PREFERENCE,
    INSPECT_PREFERENCES,
    LIST_REPORTS,
    LOAD_CURRENCY_TOOLS,
    LOAD_DELETION_TOOLS,
    LOAD_PREFERENCE_TOOLS,
    LOAD_REPORT_TOOLS,
    PROPOSE_DELETION,
    READ_REPORT,
    REMEMBER_PREFERENCE,
    SAVE_REPORT,
    SEARCH_REPORTS,
)


@dataclass(frozen=True, slots=True)
class ToolGroup:
    name: str
    loader: str
    tools: frozenset[str]
    # Request wording that makes the group relevant from the first step.
    hint: re.Pattern[str]
    # Effective preference lines that make it relevant (none: never).
    preference_hint: re.Pattern[str] | None = None


_WORDS = re.IGNORECASE

TOOL_GROUPS: tuple[ToolGroup, ...] = (
    ToolGroup(
        name="reports",
        loader=LOAD_REPORT_TOOLS,
        tools=frozenset(
            {SAVE_REPORT, READ_REPORT, LIST_REPORTS, SEARCH_REPORTS, EXPORT_REPORT}
        ),
        hint=re.compile(r"\b(reports?|save|saved|export\w*)\b", _WORDS),
    ),
    ToolGroup(
        name="report_deletion",
        loader=LOAD_DELETION_TOOLS,
        tools=frozenset({PROPOSE_DELETION, LIST_REPORTS, SEARCH_REPORTS}),
        hint=re.compile(r"\b(delet\w*|remove|erase|discard)\b", _WORDS),
    ),
    ToolGroup(
        name="preferences",
        loader=LOAD_PREFERENCE_TOOLS,
        tools=frozenset(
            {
                INSPECT_PREFERENCES,
                REMEMBER_PREFERENCE,
                FORGET_PREFERENCE,
                CONFIRM_PREFERENCE,
                DECLINE_PREFERENCE,
            }
        ),
        hint=re.compile(
            r"\b(remember\w*|forget\w*|preferences?|preferred|from now on|"
            r"by default|always use|stop using)\b",
            _WORDS,
        ),
    ),
    ToolGroup(
        name="currency",
        loader=LOAD_CURRENCY_TOOLS,
        tools=frozenset({CONVERT_CURRENCY}),
        hint=re.compile(
            r"\b(currenc\w*|convert\w*|exchange rates?|euros?|eur|gbp|pounds|"
            r"sterling|yen|jpy|usd|dollars?|cad|aud|chf|francs?)\b",
            _WORDS,
        ),
        preference_hint=re.compile(r"^display_currency\b"),
    ),
)
LOADERS: frozenset[str] = frozenset(group.loader for group in TOOL_GROUPS)
_GROUPED: frozenset[str] = frozenset().union(*(g.tools for g in TOOL_GROUPS))


def select_tools(
    authorized: Collection[str],
    *,
    request: str,
    preferences: Iterable[str] = (),
    loaded: Collection[str] = (),
) -> tuple[frozenset[str], ToolFocus]:
    """The tool names to expose now (a subset of ``authorized``) and why.

    ``loaded``: loader capabilities that succeeded earlier in this run.
    """
    allowed = frozenset(authorized)
    exposed = set(allowed - _GROUPED - LOADERS)
    preference_lines = tuple(preferences)
    active: list[tuple[str, FocusReason]] = []
    loadable: list[str] = []
    for group in TOOL_GROUPS:
        available = group.tools & allowed
        if not available:
            continue
        reason = _reason(group, request, preference_lines, loaded)
        if reason is None:
            if group.loader in allowed:
                exposed.add(group.loader)
                loadable.append(group.name)
            continue
        exposed |= available
        active.append((group.name, reason))
    chosen = frozenset(exposed)
    return chosen, ToolFocus(
        active=tuple(active),
        loadable=tuple(loadable),
        authorized=len(allowed),
        exposed=len(chosen),
    )


def _reason(
    group: ToolGroup,
    request: str,
    preferences: tuple[str, ...],
    loaded: Collection[str],
) -> FocusReason | None:
    if group.loader in loaded:
        return FocusReason.LOADED
    if group.hint.search(request):
        return FocusReason.REQUEST
    if group.preference_hint is not None and any(
        group.preference_hint.search(line) for line in preferences
    ):
        return FocusReason.PREFERENCES
    return None
