"""The bundled analytical skills, every shipped version (trusted configuration).

One module per skill; each keeps all of its shipped versions so a run that
loaded version N keeps rendering version N (``application.tool_focus``).
Never loaded from files, users, personas, Golden examples or tool results.
A new version is a new constant appended to the module's ``VERSIONS``; an
existing one is never edited after release.
"""

from __future__ import annotations

from retail_analytics.application.contracts.skills import AnalyticalSkill
from retail_analytics.application.skill_assets import (
    currency_conversion,
    investigation,
    preferences,
    saved_reports,
)

# Catalog order (also the order of the model-facing catalog).
BUNDLED: tuple[tuple[AnalyticalSkill, ...], ...] = (
    investigation.VERSIONS,
    saved_reports.VERSIONS,
    preferences.VERSIONS,
    currency_conversion.VERSIONS,
)

__all__ = ["BUNDLED"]
