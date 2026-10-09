"""Which authorized tools one model request sees: core tools plus loaded skills.

The permission-filtered catalog (``CapabilityRegistry.catalog``) is what an
executive *may* use. Most requests need its core: schema, query and evidence
tools. Specialized tools belong to four bundled, versioned analytical skills
(``application.skill_assets``): investigation, saved_reports, preferences and
currency_conversion. A run starts with the core tools, ``load_skill`` and a
short catalog of the skills the executive may use, and no active skill:

- ``load_skill(name)`` records an activation for the run (a pending operation
  record pinned to the skill's current version). The next model step makes
  it effective, so the skill's authorized tools and rendered instructions
  appear on the next turn, never in the response that loaded it;
- calls made in the same response as the load, or tools only seen in history,
  are checked against the tools in effect and refused with an actionable
  result (``SkillActivations.blocked``, called by the tool runner);
- activations stay for the run (retries, restarts, context restarts, eviction)
  and never carry over: a new run, including the one an explicit topic reset
  starts, begins with core tools again;
- a skill grants nothing: exposure and execution intersect the catalog under
  current authority every time, and the rendered instructions drop guidance
  for tools the executive cannot use (``AnalyticalSkill.render``).

Skill definitions come only from bundled code. Request, persona, Golden and
tool-result text never defines, loads or widens a skill; the selection is a
pure function of the authorized tool names and the run's recorded
activations, so the local and Temporal runtimes compute it identically inside
the step activity. Without ``load_skill`` in the catalog (a hand-built test
registry) nothing is focused: the whole authorized catalog is exposed.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from retail_analytics.application.contracts.investigations import ToolFocus
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.skills import (
    AnalyticalSkill,
    SkillLoadOutcome,
    SkillPrompt,
)
from retail_analytics.application.contracts.telemetry import Span
from retail_analytics.application.investigation_policy import LOAD_SKILL
from retail_analytics.application.ports.persistence import ToolExecutionRepository
from retail_analytics.application.skill_assets import BUNDLED
from retail_analytics.application.telemetry import telemetry
from retail_analytics.application.tools import ToolDescriptor
from retail_analytics.domain.executions import ToolExecution, ToolExecutionStatus
from retail_analytics.domain.investigations import operation_id_for
from retail_analytics.domain.operations import SideEffect

# Every shipped version, by id; the last one is what a new load pins.
SKILL_VERSIONS: Mapping[str, Mapping[int, AnalyticalSkill]] = {
    versions[-1].skill_id: {s.version: s for s in versions} for versions in BUNDLED
}
CURRENT: Mapping[str, AnalyticalSkill] = {
    versions[-1].skill_id: versions[-1] for versions in BUNDLED
}
SKILL_IDS: tuple[str, ...] = tuple(CURRENT)
SKILL_TOOLS: frozenset[str] = frozenset().union(*(s.tools for s in CURRENT.values()))
_OWNER: Mapping[str, str] = {
    tool: skill.skill_id for skill in CURRENT.values() for tool in skill.tools
}
# T26-F5 group loaders recorded by runs that started before skills existed:
# such a run keeps what it loaded (version 1 of the matching skill).
LEGACY_LOADERS: Mapping[str, str] = {
    "load_report_tools": "saved_reports",
    "load_deletion_tools": "saved_reports",
    "load_preference_tools": "preferences",
    "load_currency_tools": "currency_conversion",
}
_ACTIVATION = "skill."


@dataclass(frozen=True, slots=True)
class ToolSelection:
    tools: frozenset[str]
    focus: ToolFocus
    prompt: SkillPrompt


def owner_of(tool: str) -> str | None:
    """The skill a specialized tool belongs to (None: a core tool)."""
    return _OWNER.get(tool)


def available_skills(authorized: Collection[str]) -> tuple[AnalyticalSkill, ...]:
    """Current versions of the skills this principal may load, catalog order."""
    allowed = frozenset(authorized)
    if LOAD_SKILL not in allowed:
        return ()
    return tuple(s for s in CURRENT.values() if s.available(allowed))


def select_tools(
    authorized: Collection[str], active: Mapping[str, int] | None = None
) -> ToolSelection:
    """The tools to expose now (a subset of ``authorized``), the skill prompt
    and a summary for the trace. ``active``: effective skills -> pinned
    version, read from the run's operations (``SkillActivations``)."""
    allowed = frozenset(authorized)
    if LOAD_SKILL not in allowed:
        return ToolSelection(
            allowed,
            ToolFocus(authorized=len(allowed), exposed=len(allowed)),
            SkillPrompt(),
        )
    available = available_skills(allowed)
    loaded = dict(active or {})
    pinned = [
        SKILL_VERSIONS[s.skill_id].get(loaded[s.skill_id], s)
        for s in available
        if s.skill_id in loaded
    ]
    exposed = set(allowed - SKILL_TOOLS)
    for skill in pinned:
        exposed |= skill.tools & allowed
    if not available:
        exposed.discard(LOAD_SKILL)
    chosen = frozenset(exposed)
    loadable = tuple(s for s in available if s.skill_id not in loaded)
    return ToolSelection(
        chosen,
        ToolFocus(
            active=tuple((s.skill_id, s.version) for s in pinned),
            loadable=tuple(s.skill_id for s in loadable),
            authorized=len(allowed),
            exposed=len(chosen),
        ),
        SkillPrompt(
            catalog=tuple((s.skill_id, s.description) for s in loadable),
            active=tuple((s.skill_id, s.version, s.render(chosen)) for s in pinned),
        ),
    )


def focus_catalog(
    descriptors: Sequence[ToolDescriptor],
) -> tuple[ToolDescriptor, ...]:
    """The model-facing schemas: ``load_skill`` accepts only the skill ids
    this principal may load now (an enum, no path, prompt or tool list)."""
    names = available_skills([d.name for d in descriptors])
    out: list[ToolDescriptor] = []
    for descriptor in descriptors:
        if descriptor.name == LOAD_SKILL:
            if not names:
                continue
            descriptor = descriptor.model_copy(
                update={"parameters": _load_parameters(names)}
            )
        out.append(descriptor)
    return tuple(out)


def _load_parameters(skills: Iterable[AnalyticalSkill]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": [s.skill_id for s in skills],
                "description": "A skill id from the skill catalog.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    }


def activation_id(run_id: str, skill_id: str) -> str:
    """One activation record per run and skill: repeated loads are idempotent
    and keep the version pinned by the first one."""
    return operation_id_for(run_id, f"{_ACTIVATION}{skill_id}")


def effective_skills(operations: Iterable[ToolExecution]) -> dict[str, int]:
    """Skills in effect for the run: activations a model step has seen."""
    active: dict[str, int] = {}
    for op in operations:
        if op.status is not ToolExecutionStatus.SUCCEEDED:
            continue
        if op.capability.startswith(_ACTIVATION):
            active.setdefault(
                op.capability.removeprefix(_ACTIVATION), op.capability_version
            )
        elif op.capability in LEGACY_LOADERS:
            active.setdefault(LEGACY_LOADERS[op.capability], 1)
    return {k: v for k, v in active.items() if k in CURRENT}


@dataclass(frozen=True, slots=True)
class SkillLoad:
    outcome: SkillLoadOutcome
    skill: AnalyticalSkill | None = None
    # Tools the executive may call once the skill is in effect.
    tools: tuple[str, ...] = ()
    instructions: str = ""
    # Loaded in this response: callable from the next model turn.
    pending: bool = True
    available: tuple[str, ...] = ()


class SkillActivations:
    """The run's skill activations, kept in its operation records (the
    existing execution facility shared by every runtime)."""

    def __init__(self, operations: ToolExecutionRepository) -> None:
        self._operations = operations

    async def effective(self, run_id: str) -> dict[str, int]:
        return effective_skills(await self._operations.for_run(run_id))

    async def take_effect(self, run_id: str) -> dict[str, int]:
        """Called when a model step is prepared: activations recorded since
        the previous step take effect for this and later steps."""
        operations = await self._operations.for_run(run_id)
        for op in operations:
            if (
                op.capability.startswith(_ACTIVATION)
                and op.status is ToolExecutionStatus.PREPARED
            ):
                await self._operations.transition(
                    op.operation_id, ToolExecutionStatus.SUCCEEDED, attempt=1
                )
        return effective_skills(await self._operations.for_run(run_id))

    async def load(
        self, run_id: str, name: str, authorized: Collection[str]
    ) -> SkillLoad:
        """Record ``name`` for the run if the executive may use it now."""
        allowed = frozenset(authorized)
        available = available_skills(allowed)
        skill = next((s for s in available if s.skill_id == name), None)
        if skill is None:
            # Unknown and unavailable are indistinguishable.
            load = SkillLoad(
                SkillLoadOutcome.REJECTED,
                available=tuple(s.skill_id for s in available),
            )
            _trace(run_id, name if name in CURRENT else "unknown", load)
            return load
        start = await self._operations.begin(
            OperationRequest(
                operation_id=activation_id(run_id, skill.skill_id),
                run_id=run_id,
                capability=f"{_ACTIVATION}{skill.skill_id}",
                capability_version=skill.version,
                side_effect=SideEffect.READ_ONLY,
            )
        )
        record = start.execution
        pinned = SKILL_VERSIONS[skill.skill_id].get(record.capability_version, skill)
        effective = effective_skills(await self._operations.for_run(run_id))
        selection = select_tools(allowed, {**effective, skill.skill_id: pinned.version})
        load = SkillLoad(
            SkillLoadOutcome.LOADED
            if start.created
            else SkillLoadOutcome.ALREADY_ACTIVE,
            pinned,
            tuple(sorted(pinned.tools & selection.tools)),
            pinned.render(selection.tools),
            pending=record.status is not ToolExecutionStatus.SUCCEEDED,
        )
        _trace(run_id, skill.skill_id, load)
        return load

    async def blocked(
        self, run_id: str, tool: str, authorized: Collection[str]
    ) -> str | None:
        """The skill to load before ``tool`` may run in this run, or None.

        Only for an authorized specialized tool whose skill is not in effect
        yet (never loaded, or loaded in the same model response). Seeing the
        tool in history, or an activation that is still pending, is not
        enough.
        """
        skill_id = owner_of(tool)
        if skill_id is None or LOAD_SKILL not in authorized:
            return None
        if skill_id in await self.effective(run_id):
            return None
        _trace(
            run_id,
            skill_id,
            SkillLoad(SkillLoadOutcome.TOOL_BLOCKED, CURRENT[skill_id]),
            tool=tool,
        )
        return skill_id


def _trace(
    run_id: str, skill_id: str, load: SkillLoad, tool: str | None = None
) -> None:
    """Skill id, version, outcome and tool names only (no instructions text)."""
    attributes: dict[str, object] = {
        "skill.id": skill_id,
        "skill.version": load.skill.version if load.skill else 0,
        "skill.outcome": load.outcome.value,
    }
    if tool is not None:
        attributes["skill.blocked_tool"] = tool
    with telemetry().span(Span.SKILL, run_id=run_id, attributes=attributes) as span:
        if span.captures:
            span.outputs({"tools": list(load.tools), "pending": load.pending})
