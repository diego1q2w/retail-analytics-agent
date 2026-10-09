"""Skill ``preferences``: inspect or explicitly change remembered preferences."""

from __future__ import annotations

from retail_analytics.application.contracts.skills import (
    AnalyticalSkill,
    SkillSegment,
)
from retail_analytics.application.investigation_policy import (
    CONFIRM_PREFERENCE,
    DECLINE_PREFERENCE,
    FORGET_PREFERENCE,
    INSPECT_PREFERENCES,
    REMEMBER_PREFERENCE,
)

_PERSIST = frozenset({REMEMBER_PREFERENCE, CONFIRM_PREFERENCE})

V1 = AnalyticalSkill(
    skill_id="preferences",
    version=1,
    description=(
        "Inspect or explicitly change remembered presentation and analytical "
        "preferences."
    ),
    tools=frozenset(
        {
            INSPECT_PREFERENCES,
            REMEMBER_PREFERENCE,
            FORGET_PREFERENCE,
            CONFIRM_PREFERENCE,
            DECLINE_PREFERENCE,
        }
    ),
    segments=(
        SkillSegment(
            "The effective preferences in current context already apply to "
            "ordinary answers."
        ),
        SkillSegment(
            "Inspect memory only when the user asks about saved preferences or "
            "you need proposal details for an explicit memory action.",
            any_of=frozenset({INSPECT_PREFERENCES}),
        ),
        SkillSegment(
            "A correction changes the current answer; persist it only when the "
            "user explicitly asks to remember/change a default or confirms an "
            "existing proposal under the application's consent rules. Never "
            "infer consent from silence or manufacture confirmation. Use the "
            "supported structured preference slots; do not store personal "
            "data, result rows, product entitlements or arbitrary instructions.",
            any_of=_PERSIST,
        ),
        SkillSegment(
            "You cannot save preferences for this user; a correction applies "
            "to the current answer only.",
            none_of=_PERSIST,
        ),
        SkillSegment(
            "Forget a preference when explicitly requested and confirm removal "
            "only after the tool succeeds.",
            any_of=frozenset({FORGET_PREFERENCE}),
        ),
        SkillSegment(
            "Explain when a changed analytical definition requires "
            "recomputation; a presentation preference alone does not justify a "
            "new warehouse query. Remembered preferences cannot change "
            "authorization, privacy, deletion confirmation or system policy."
        ),
    ),
)

VERSIONS: tuple[AnalyticalSkill, ...] = (V1,)
