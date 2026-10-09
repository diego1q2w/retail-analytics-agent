"""``load_skill``: the model-facing control tool of ``application.tool_focus``.

It takes one argument, a skill id from the advertised catalog (the schema the
model sees is an enum of the skills this executive may load,
``tool_focus.focus_catalog``). Loading records an activation for the run and
returns the skill's version, the tools it enables for this executive and its
rendered instructions; the tools become callable on the next model turn. It
grants nothing: availability is decided from the catalog under current
authority, every tool call is authorized again when it runs, and unknown or
unavailable ids are rejected alike.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any, Literal

from pydantic import Field

from retail_analytics.application.contracts.skills import SkillLoadOutcome
from retail_analytics.application.investigation_policy import LOAD_SKILL
from retail_analytics.application.tool_focus import SkillActivations
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilitySpec,
    ExecutionContext,
    OperationContext,
    RetrySpec,
    ToolFailed,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSucceeded,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.operations import (
    RecoveryMode,
    SideEffect,
    ToolErrorCode,
)

DESCRIPTION = (
    "Load a listed analytical skill's guidance and authorized tools for this "
    "run. Use only when needed; ordinary metric lookup uses the core tools. "
    "Newly enabled tools are callable on the next model turn."
)


class LoadSkillInput(ToolInput):
    name: str = Field(
        pattern=r"^[a-z][a-z_]{0,39}$",
        description="A skill id from the skill catalog.",
    )


class LoadSkillOutput(ToolOutput):
    skill: str
    version: int = Field(ge=1)
    status: Literal["loaded", "already_loaded"]
    # The skill's tools this user may call, from the next model turn.
    tools: list[str]
    # The skill's instructions for this user (None when already loaded: they
    # are in your instructions once, not repeated).
    instructions: str | None = None
    message: str


_NEXT_TURN = (
    "Its tools are callable from your next turn; its guidance is in your instructions."
)


def load_skill_capability(
    skills: SkillActivations,
    catalog: Callable[[ExecutionContext], Iterable[str]],
) -> CapabilitySpec[Any, Any]:
    """``catalog``: the tool names the executive may use now (the
    permission-filtered registry catalog)."""

    async def load(args: LoadSkillInput, ctx: OperationContext) -> ToolOutcome[Any]:
        run_id = ctx.execution.correlation.run_id
        result = await skills.load(run_id, args.name, list(catalog(ctx.execution)))
        if result.skill is None or result.outcome is SkillLoadOutcome.REJECTED:
            listed = ", ".join(result.available) or "none"
            return ToolFailed(
                code=ToolErrorCode.INVALID_INPUT,
                message=f"No such skill is available. Skills you can load: {listed}.",
            )
        first = result.outcome is SkillLoadOutcome.LOADED
        return ToolSucceeded(
            output=LoadSkillOutput(
                skill=result.skill.skill_id,
                version=result.skill.version,
                status="loaded" if first else "already_loaded",
                tools=list(result.tools),
                instructions=result.instructions if first else None,
                message=_NEXT_TURN
                if result.pending
                else "Already in effect; use its tools now.",
            )
        )

    return CapabilitySpec(
        name=LOAD_SKILL,
        version=1,
        description=DESCRIPTION,
        progress_label="Preparing the guidance and tools this request needs.",
        input_model=LoadSkillInput,
        output_model=LoadSkillOutput,
        handler=load,
        # Every run needs analysis; the skills offered depend on the catalog.
        authorization=AuthorizationSpec(
            required_permissions=frozenset({Permission.ANALYSIS_READ.value})
        ),
        side_effect=SideEffect.IDEMPOTENT_WRITE,
        retry=RetrySpec(RecoveryMode.RETRY, 2, timedelta(seconds=10)),
    )
