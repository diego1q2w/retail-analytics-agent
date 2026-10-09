"""Skill ``saved_reports``: create, find, read, export and propose deletion."""

from __future__ import annotations

from retail_analytics.application.contracts.skills import (
    AnalyticalSkill,
    SkillSegment,
)
from retail_analytics.application.investigation_policy import (
    EXPORT_REPORT,
    LIST_REPORTS,
    PROPOSE_DELETION,
    READ_REPORT,
    SAVE_REPORT,
    SEARCH_REPORTS,
)

_SAVE = frozenset({SAVE_REPORT})
_DELETE = frozenset({PROPOSE_DELETION})

V1 = AnalyticalSkill(
    skill_id="saved_reports",
    version=1,
    description=(
        "Create, find, read or export your saved reports; propose deletion "
        "only when requested."
    ),
    tools=frozenset(
        {
            SAVE_REPORT,
            READ_REPORT,
            LIST_REPORTS,
            SEARCH_REPORTS,
            EXPORT_REPORT,
            PROPOSE_DELETION,
        }
    ),
    segments=(
        SkillSegment(
            "Manage reports only when the user asks to create, revisit, export "
            "or delete them. Do not list reports as preparation for an "
            "unrelated analysis."
        ),
        SkillSegment(
            "Resolve report references from the conversation; use search or "
            "listing only when needed to identify the requested report.",
            any_of=frozenset({LIST_REPORTS, SEARCH_REPORTS}),
        ),
        SkillSegment(
            "Reading a report does not prove its figures are current: follow "
            "its definition notices, evidence-reuse eligibility and source "
            "dates.",
            any_of=frozenset({READ_REPORT}),
        ),
        SkillSegment(
            "To create a report, use authorized supported findings with "
            "evidence citations, state the metric definitions and periods, and "
            "distinguish recommended actions from measured findings. Match the "
            "scope and detail requested. Write amounts by the currency rule in "
            "Analytical rules: keep the known currency or state that it is "
            "unverified; never infer a dollar sign from an unlabelled amount. "
            f"Save only through {SAVE_REPORT}, and claim success only after a "
            "successful result.",
            any_of=_SAVE,
        ),
        SkillSegment(
            "You cannot save reports for this user; give the findings in the "
            "answer instead.",
            none_of=_SAVE,
        ),
        SkillSegment(
            "For deletion, resolve the user's requested set and propose "
            f"deletion ({PROPOSE_DELETION}). The application shows the exact "
            "impact and collects explicit human confirmation. You cannot "
            "confirm, bypass or simulate that confirmation, and you cannot "
            "restore reports.",
            any_of=_DELETE,
        ),
        SkillSegment(
            "You cannot delete reports for this user; deletion is never "
            "confirmed by chat.",
            none_of=_DELETE,
        ),
        SkillSegment(
            "If a needed operation is unavailable under current permissions, "
            "explain that limitation without attempting it."
        ),
    ),
)

VERSIONS: tuple[AnalyticalSkill, ...] = (V1,)
