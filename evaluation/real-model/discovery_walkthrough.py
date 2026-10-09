"""Discovery walkthrough: what a first conversation costs with the real model.

Runs three short conversations through the ``agent_runtime`` target (real
investigation runtime, local backend, offline DuckDB over the frozen extract,
live provider chain), each in a fresh evaluation session:

- ``overview``: "What data do you have, and what questions can you help me
  answer?" then "orders" (answers an open question, else a follow-up);
- ``profiling``: an explicit count and date-range question, which may query;
- ``followup``: a scalar revenue question and its "And November?" follow-up,
  to count schema discovery calls (``list_relations``/``describe_relation``)
  per run and the input tokens they cost.

Prints, per run: admission outcome (asked or not), tool sequence, provider,
model requests, tokens, queries, active seconds and wall-clock latency. No
answer text is written anywhere; ``--show-answers`` prints the released
answers to the terminal for a reviewer. Run from the repository root that
holds the ``.env`` (see ``README.md`` here); about 10-20 model requests.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from retail_analytics.adapters.evaluation.files import load_manifest
from retail_analytics.adapters.evaluation.telemetry_recorder import (
    RecordingTelemetrySink,
)
from retail_analytics.application.contracts.evaluation import (
    ConversationRecord,
    ScenarioInput,
    Turn,
)
from retail_analytics.application.contracts.progress import EventKind
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.bootstrap.agent_evaluation import (
    AgentRuntimeTarget,
    realdata_source,
)
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.domain.runs import ExecutionBackend

HERE = Path(__file__).resolve().parent
EVALUATION = HERE.parent
CONVERSATIONS = {
    "overview": (
        "What data do you have, and what questions can you help me answer?",
        "orders",
    ),
    "profiling": (
        "How many orders are there, and what date range does the data cover?",
    ),
    "followup": (
        "What was our total revenue in October 2025? Just the total, please.",
        "And November?",
    ),
}
DISCOVERY_TOOLS = frozenset({"list_relations", "describe_relation"})
# Any frozen-extract scenario's executive and product scope will do.
SCOPE_FROM = "rd-l1-product-top3-q4"


@dataclass
class _Run:
    run_id: str
    status: str
    asked: bool
    tools: list[str]
    requests: int
    tokens: int
    queries: int
    active_seconds: float
    wall_seconds: float


@dataclass
class WalkthroughTarget(AgentRuntimeTarget):
    runs: list[_Run] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)

    async def _record(
        self, harness: Any, principal: Any, session_id: str, run_ids: Sequence[str]
    ) -> ConversationRecord:
        record = await super()._record(harness, principal, session_id, run_ids)
        db = harness.persistence
        now = datetime.now(UTC)
        for run_id in run_ids:
            run = await db.runs.get_run(run_id)
            events = await db.run_events.replay(run_id, limit=10_000)
            tools = [
                e.tool.capability
                for e in events
                if e.tool is not None and e.kind is EventKind.TOOL_STARTED
            ]
            budget = await db.budgets.get(run_id)
            usage = budget.usage if budget is not None else None
            ended = (run.completed_at or run.updated_at) if run else now
            self.runs.append(
                _Run(
                    run_id=run_id,
                    status=run.status.value if run else "missing",
                    asked=any(e.kind is EventKind.INPUT_REQUIRED for e in events),
                    tools=tools,
                    requests=usage.provider_requests if usage else 0,
                    tokens=usage.tokens if usage else 0,
                    queries=usage.queries if usage else 0,
                    active_seconds=round(usage.active_seconds(now), 1) if usage else 0,
                    wall_seconds=round((ended - run.created_at).total_seconds(), 1)
                    if run
                    else 0.0,
                )
            )
        self.answers = list(record.answers)
        return record


def _span_tokens(sink: RecordingTelemetrySink, run_id: str) -> dict[str, Any]:
    used: dict[str, Any] = {"input": 0, "output": 0, "providers": set()}
    for span in sink.spans():
        if span.run_id != run_id or span.name != "model.attempt":
            continue
        attrs = span.attributes
        if attrs.get("outcome") == "succeeded":
            used["providers"].add(f"{attrs.get('provider')}:{attrs.get('model')}")
            used["input"] += int(attrs.get("input_tokens", 0) or 0)
            used["output"] += int(attrs.get("output_tokens", 0) or 0)
    used["providers"] = sorted(used["providers"])
    return used


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=sorted(CONVERSATIONS), nargs="*")
    parser.add_argument("--show-answers", action="store_true")
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    settings = load_backend_settings()
    if settings.gemini_api_key is None:
        print("blocked: no Gemini key configured; nothing was run")
        return 3
    manifest = load_manifest(EVALUATION / "realdata/manifest.json")
    scope = next(s for s in manifest.scenarios if s.id == SCOPE_FROM).scope
    sink = RecordingTelemetrySink()
    report: dict[str, Any] = {}
    with use_telemetry(Telemetry(sink)):
        target = WalkthroughTarget(
            settings,
            realdata_source(EVALUATION),
            provider_chain(settings),
            turn_timeout=args.turn_timeout,
            backend=ExecutionBackend.LOCAL,
        )
        try:
            for name in args.only or list(CONVERSATIONS):
                target.runs, target.answers = [], []
                started = time.monotonic()
                target.run(
                    ScenarioInput(
                        scenario_id=f"walkthrough-{name}",
                        mode="fixture",
                        fixture_ref=None,
                        scope=scope,
                        dialogue=tuple(Turn(text=t) for t in CONVERSATIONS[name]),
                    )
                )
                runs = []
                for run in target.runs:
                    tokens = _span_tokens(sink, run.run_id)
                    runs.append(
                        {
                            "status": run.status,
                            "asked_a_question": run.asked,
                            "tools": run.tools,
                            "discovery_calls": sum(
                                t in DISCOVERY_TOOLS for t in run.tools
                            ),
                            "providers": tokens["providers"],
                            "model_requests": run.requests,
                            "tokens_budget": run.tokens,
                            "tokens_in": tokens["input"],
                            "tokens_out": tokens["output"],
                            "queries": run.queries,
                            "active_seconds": run.active_seconds,
                            "wall_seconds": run.wall_seconds,
                        }
                    )
                report[name] = {
                    "conversation_seconds": round(time.monotonic() - started, 1),
                    "runs": runs,
                }
                if args.show_answers:
                    for index, answer in enumerate(target.answers, 1):
                        print(f"--- {name} assistant message {index} ---\n{answer}\n")
        finally:
            target.close()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
