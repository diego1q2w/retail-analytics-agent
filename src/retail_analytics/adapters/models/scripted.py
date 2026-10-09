"""A scripted stand-in for the provider model, for deterministic evaluation.

It plays a reviewed plan instead of reasoning: for the request it is given it
emits the plan's tool calls one by one, then the plan's answer (or
clarification). Everything else is real: the guarded model step (fresh
authority, context assembly, budget accounting), the permission-filtered
catalog, the single tool path, the compiler, the privacy boundary, evidence,
reports and the output gate. Plans can therefore be *adversarial* (attempt a
raw table, out-of-scope products or a deletion approval) to show that guards
hold no matter what a model asks for.

A scripted run measures the runtime and its guards with a known plan; it says
nothing about how well a real model would plan. Results must be reported as
scripted.

Plan format (``plans`` maps the exact user message text to steps)::

    {"call": "<tool>", "args": {...}}           a tool call
    {"call": ..., "otherwise": {<step>}}        fallback when the tool is
                                                not in the catalog
    {"answer": {"text": "...", "cite": ["<column>", ...], "complete": true}}
    {"ask": "<question>"}

Any step may carry ``"delay_seconds"`` (the model waits before replying). When
later user messages (steering) have their own plan, the latest such plan is
played from its start.

Strings may contain placeholders resolved from the evidence the model sees in
its context (newest first): ``{{value:<column>}}`` (first row) or
``{{value:<column>#<row>}}``, and ``{{evidence:<column>}}`` for the id of the
evidence holding that column. ``cite`` lists columns whose evidence the answer
cites. An unresolvable placeholder renders as ``unavailable``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

SCRIPTED_MODEL_NAME = "scripted-plan"
UNAVAILABLE = "unavailable"
_PLACEHOLDER = re.compile(r"\{\{(value|evidence):([A-Za-z0-9_]+)(?:#(\d+))?\}\}")
_HEADER = re.compile(r"^evidence (evd_[0-9a-z]+) v\d+;")
_UNPLANNED = (
    "I could not complete this request with the scripted plan available for evaluation."
)


@dataclass(frozen=True, slots=True)
class _Evidence:
    evidence_id: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def _section(text: str, tag: str) -> str:
    start = text.rfind(f"<{tag}>")
    end = text.rfind(f"</{tag}>")
    if start < 0 or end < start:
        return ""
    return text[start + len(tag) + 2 : end]


def parse_evidence(instructions: str) -> list[_Evidence]:
    """Evidence blocks rendered by the context builder, newest first."""
    found: list[_Evidence] = []
    current: dict[str, Any] | None = None
    for line in _section(instructions, "evidence").splitlines():
        header = _HEADER.match(line)
        if header:
            if current is not None:
                found.append(_Evidence(**current))
            current = {"evidence_id": header.group(1), "columns": (), "rows": ()}
            continue
        if current is None or not line.strip():
            continue
        if line.startswith("columns: "):
            current["columns"] = tuple(line[len("columns: ") :].split(" | "))
        elif line.startswith(("note: ", "... ", "rows omitted")):
            continue
        elif current["columns"]:
            cells = tuple(line.split(" | "))
            if len(cells) == len(current["columns"]):
                current["rows"] = (*current["rows"], cells)
    if current is not None:
        found.append(_Evidence(**current))
    return found


def _resolve(text: str, evidence: Sequence[_Evidence]) -> str:
    def lookup(match: re.Match[str]) -> str:
        kind, column, row = match.group(1), match.group(2), match.group(3)
        for item in evidence:
            if column not in item.columns:
                continue
            if kind == "evidence":
                return item.evidence_id
            index = int(row or 0)
            if index >= len(item.rows):
                return UNAVAILABLE
            return item.rows[index][item.columns.index(column)]
        return UNAVAILABLE

    return _PLACEHOLDER.sub(lookup, text)


def _fill(value: Any, evidence: Sequence[_Evidence]) -> Any:
    if isinstance(value, str):
        return _resolve(value, evidence)
    if isinstance(value, list):
        filled = [_fill(v, evidence) for v in value]
        # A list of evidence ids drops the ones that could not be resolved.
        return [v for v in filled if v != UNAVAILABLE]
    if isinstance(value, dict):
        return {k: _fill(v, evidence) for k, v in value.items()}
    return value


def _plan_for(
    request: str, plans: Mapping[str, Sequence[Mapping[str, Any]]]
) -> list[Mapping[str, Any]]:
    """The plan for the latest message that has one (steering wins).

    The request block lists the run's own message first, then each later
    message prefixed with a bracketed note; a later message with a plan
    replaces the original plan, like steering redirects an agent.
    """
    parts = [p.strip() for p in request.strip().split("\n\n") if p.strip()]
    for part in reversed(parts):
        text = part.split("] ", 1)[1] if part.startswith("[") and "] " in part else part
        found = plans.get(" ".join(text.split()))
        if found is not None:
            return list(found)
    return []


def _output_tool(info: AgentInfo, marker: str) -> str:
    for tool in info.output_tools:
        if marker in tool.name:
            return tool.name
    raise RuntimeError(f"no output tool for {marker}")


def scripted_model(plans: Mapping[str, Sequence[Mapping[str, Any]]]) -> FunctionModel:
    """A model that plays ``plans`` (user message text -> steps)."""
    normalized = {" ".join(k.split()): list(v) for k, v in plans.items()}

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        instructions = "\n".join(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, SystemPromptPart)
        )
        request = _section(instructions, "request")
        # The run's own message comes first; later steering follows it.
        steps = _plan_for(request, normalized)
        taken = sum(isinstance(m, ModelResponse) for m in messages)
        evidence = parse_evidence(instructions)
        step = steps[taken] if taken < len(steps) else None
        if step is None:
            step = {"answer": {"text": _UNPLANNED, "complete": False}}
        if "call" in step and "otherwise" in step:
            available = {tool.name for tool in info.function_tools}
            if step["call"] not in available:
                # A plan's fallback when the catalog does not offer the tool.
                step = step["otherwise"]
        if "delay_seconds" in step:
            # Lets a test deliver steering while this run is still working.
            await asyncio.sleep(float(step["delay_seconds"]))
        if "call" in step:
            arguments = _fill(dict(step.get("args", {})), evidence)
            # Tool call IDs key durable operations: distinct calls (also after
            # a conversation restart) need distinct IDs, repeats the same one.
            fingerprint = hashlib.sha256(
                json.dumps(
                    [request, taken, step["call"], arguments], sort_keys=True
                ).encode()
            ).hexdigest()[:16]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        str(step["call"]),
                        arguments,
                        tool_call_id=f"scripted-{fingerprint}",
                    )
                ]
            )
        if "ask" in step:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        _output_tool(info, "Clarification"),
                        {"question": _resolve(str(step["ask"]), evidence)},
                    )
                ]
            )
        answer = step["answer"]
        cited = [
            _resolve(f"{{{{evidence:{column}}}}}", evidence)
            for column in answer.get("cite", [])
        ]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    _output_tool(info, "Answer"),
                    {
                        "text": _resolve(str(answer["text"]), evidence),
                        "cited_evidence": sorted(
                            {c for c in cited if c != UNAVAILABLE}
                        ),
                        "complete": bool(answer.get("complete", True)),
                    },
                )
            ]
        )

    return FunctionModel(respond, model_name=SCRIPTED_MODEL_NAME)


__all__ = ["SCRIPTED_MODEL_NAME", "parse_evidence", "scripted_model"]
