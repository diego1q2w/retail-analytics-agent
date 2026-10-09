"""Skill ``currency_conversion``: evidence-backed conversion on request."""

from __future__ import annotations

from retail_analytics.application.contracts.skills import (
    AnalyticalSkill,
    SkillSegment,
)
from retail_analytics.application.investigation_policy import CONVERT_CURRENCY

V1 = AnalyticalSkill(
    skill_id="currency_conversion",
    version=1,
    description=(
        "Convert evidence-backed amounts to a requested currency using an "
        "explicit source currency and rate basis."
    ),
    tools=frozenset({CONVERT_CURRENCY}),
    segments=(
        SkillSegment(
            "Convert amounts only when the user requests a different currency "
            "(a saved display-currency preference is such a request). Use an "
            "authorized evidence reference and its numeric amount columns. "
            "Establish the source currency from trusted metadata or an "
            "explicit declaration accepted by the existing currency tool; "
            "never guess it from a symbol, country or dataset name. If it is "
            "unverified, ask the necessary focused question or state why "
            "conversion is blocked."
        ),
        SkillSegment(
            "Resolve the target currency and the tool's supported current or "
            "historical rate basis, stating the chosen rate date. Preserve "
            "original amounts and cite the derived evidence. Explain the rate "
            "source and basis briefly. Do not relabel unchanged amounts as "
            "converted or claim an unavailable rate was applied; write amounts "
            "by the currency rule in Analytical rules. Reuse existing evidence "
            "where permitted instead of querying the warehouse simply to "
            "change display currency."
        ),
    ),
)

VERSIONS: tuple[AnalyticalSkill, ...] = (V1,)
