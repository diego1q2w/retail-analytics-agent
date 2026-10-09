"""Analytical skills: trusted, versioned instructions plus the tools they enable.

A skill is application-owned configuration bundled with the code
(``application/skill_assets``), never user, persona, Golden or tool-result
text. It grants nothing: its tools are exposed and executed only while they
are in the principal's permission-filtered catalog, and its instructions are
rendered against the tools actually exposed (``AnalyticalSkill.render``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

_ID = re.compile(r"^[a-z][a-z_]{0,39}$")


@dataclass(frozen=True, slots=True)
class SkillSegment:
    """One paragraph of a skill's instructions and when it applies.

    ``any_of``: shown only when at least one of these tools is exposed (empty:
    always). ``none_of``: shown only when none of these tools is exposed (to
    state a limit instead of describing an unavailable operation). A segment
    names a tool only if it is gated on exactly that tool, so rendered text
    never points at something the model cannot call.
    """

    text: str
    any_of: frozenset[str] = frozenset()
    none_of: frozenset[str] = frozenset()

    def applies(self, tools: frozenset[str]) -> bool:
        if self.any_of and not self.any_of & tools:
            return False
        return not self.none_of & tools


@dataclass(frozen=True, slots=True)
class AnalyticalSkill:
    """One version of a bundled skill. Versions are immutable once shipped:
    a run keeps the version it loaded (replay and in-flight runs)."""

    skill_id: str
    version: int
    # Short catalog text the model reads before loading.
    description: str
    # The specialized tools this skill makes available (if authorized).
    tools: frozenset[str]
    segments: tuple[SkillSegment, ...]
    # Also available to principals with none of ``tools`` but any of these
    # (investigation guidance stays available to analysts without examples).
    guidance_for: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.skill_id) or self.version < 1:
            raise ValueError(f"invalid skill {self.skill_id!r} v{self.version}")
        if not self.description.strip() or not self.segments:
            raise ValueError(f"skill {self.skill_id!r} needs text")

    def available(self, authorized: frozenset[str]) -> bool:
        """Whether the principal may load it at all (any usable part)."""
        return bool((self.tools | self.guidance_for) & authorized)

    def render(self, exposed: frozenset[str]) -> str:
        """The instructions for exactly the tools exposed now."""
        return "\n".join(s.text for s in self.segments if s.applies(exposed))


class SkillLoadOutcome(StrEnum):
    """Telemetry code of one ``load_skill`` call."""

    LOADED = "loaded"
    ALREADY_ACTIVE = "already_active"
    REJECTED = "rejected"
    # A specialized tool was called before its skill took effect.
    TOOL_BLOCKED = "tool_blocked"


@dataclass(frozen=True, slots=True)
class SkillPrompt:
    """The skill part of one model request's instructions.

    ``catalog``: (id, description) of the skills that can still be loaded;
    ``active``: (id, version, rendered instructions) of the loaded ones.
    """

    catalog: tuple[tuple[str, str], ...] = ()
    active: tuple[tuple[str, int, str], ...] = ()
