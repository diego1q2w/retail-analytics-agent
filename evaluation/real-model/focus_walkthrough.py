"""Focus walkthrough: which tools each real model request is sent, and what it costs.

Runs short conversations through the ``agent_runtime`` target (real
investigation runtime, local backend, offline DuckDB over the frozen extract,
live provider chain), each in a fresh evaluation session:

- ``scalar``: "What's the latest revenue of September?" then "And August?";
- ``complex``: a "why did revenue change" investigation with a category
  breakdown;
- ``mixed``: a figure plus keeping a copy for later (worded without "report",
  so saving needs the model to broaden the tools itself), then a follow-up
  asking for another currency and to remember it (currency and preferences).

Every provider request is recorded at the provider boundary: the tool
definitions actually sent (names and the characters of their descriptions and
JSON schemas), the characters of the instructions and the provider's input and
output tokens. Prints per run: tools called, requests, tokens, queries, tools
and schema characters sent per request, and any tool-group loads. Works on
code before and after focused tool exposure (it only reads names that exist
in both). Run from the repository root that holds the ``.env`` (see
``README.md`` here) against a migrated PostgreSQL that no API holds; about
15-30 model requests.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from discovery_walkthrough import (
    EVALUATION,
    SCOPE_FROM,
    WalkthroughTarget,
    _span_tokens,
)
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, SystemPromptPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from retail_analytics.adapters.evaluation.files import load_manifest
from retail_analytics.adapters.evaluation.telemetry_recorder import (
    RecordingTelemetrySink,
)
from retail_analytics.adapters.models.budgeted import current_run_id
from retail_analytics.application.contracts.evaluation import ScenarioInput, Turn
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.bootstrap.agent_evaluation import realdata_source
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.bootstrap.models import (
    gemini_model,
    openai_model,
    provider_chain,
)
from retail_analytics.domain.runs import ExecutionBackend

CONVERSATIONS = {
    "scalar": (
        "What's the latest revenue of September?",
        "And August?",
    ),
    "complex": (
        "Why did revenue change between August and September? Break it down "
        "by category and name the main contributors.",
    ),
    "mixed": (
        "What was revenue in September? Keep a copy of the result for the "
        "board so I can find it again later.",
        "Also show that figure in the currency they use in Berlin, and "
        "remember that I prefer that currency from now on.",
    ),
}
LOADER_PREFIX = "load_"
FOCUS_SPAN = "investigation.tool_focus"


@dataclass
class _Request:
    run_id: str
    tools: list[str]
    schema_chars: int
    instruction_chars: int
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class _Requests:
    items: list[_Request] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def for_run(self, run_id: str) -> list[_Request]:
        with self.lock:
            return [r for r in self.items if r.run_id == run_id]


class RecordingProvider(WrapperModel):
    """A provider model that records what each request was sent."""

    def __init__(self, wrapped: Model, requests: _Requests) -> None:
        super().__init__(wrapped)
        self._requests = requests

    def _entry(
        self, messages: list[ModelMessage], parameters: ModelRequestParameters
    ) -> _Request:
        tools = parameters.function_tools
        return _Request(
            run_id=_run_id(),
            tools=sorted(t.name for t in tools),
            schema_chars=sum(
                len(t.description or "") + len(json.dumps(t.parameters_json_schema))
                for t in tools
            ),
            instruction_chars=sum(
                len(part.content)
                for message in messages
                for part in message.parts
                if isinstance(part, SystemPromptPart)
            ),
        )

    def _done(self, entry: _Request, usage: RequestUsage) -> None:
        entry.tokens_in = usage.input_tokens
        entry.tokens_out = usage.output_tokens
        with self._requests.lock:
            self._requests.items.append(entry)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        entry = self._entry(messages, model_request_parameters)
        response = await super().request(
            messages, model_settings, model_request_parameters
        )
        self._done(entry, response.usage)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        entry = self._entry(messages, model_request_parameters)
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream
        self._done(entry, stream.usage)


def _run_id() -> str:
    try:
        return current_run_id()
    except Exception:
        return "?"


def _focus_changes(sink: RecordingTelemetrySink, run_id: str) -> list[dict[str, Any]]:
    return [
        {
            key.removeprefix("focus."): value
            for key, value in span.attributes.items()
            if key.startswith("focus.")
        }
        for span in sink.spans()
        if span.run_id == run_id and span.name == FOCUS_SPAN
    ]


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
    requests = _Requests()
    backup = openai_model(settings)
    providers: list[Model] = [
        RecordingProvider(gemini_model(settings), requests),
        *([RecordingProvider(backup, requests)] if backup else []),
    ]
    sink = RecordingTelemetrySink()
    report: dict[str, Any] = {}
    with use_telemetry(Telemetry(sink)):
        target = WalkthroughTarget(
            settings,
            realdata_source(EVALUATION),
            provider_chain(settings, providers=providers),
            turn_timeout=args.turn_timeout,
            backend=ExecutionBackend.LOCAL,
        )
        try:
            for name in args.only or list(CONVERSATIONS):
                target.runs, target.answers = [], []
                started = time.monotonic()
                target.run(
                    ScenarioInput(
                        scenario_id=f"focus-{name}",
                        mode="fixture",
                        fixture_ref=None,
                        scope=scope,
                        dialogue=tuple(Turn(text=t) for t in CONVERSATIONS[name]),
                    )
                )
                runs = []
                for run in target.runs:
                    tokens = _span_tokens(sink, run.run_id)
                    sent = requests.for_run(run.run_id)
                    runs.append(
                        {
                            "status": run.status,
                            "tools_called": run.tools,
                            "loads": [
                                t for t in run.tools if t.startswith(LOADER_PREFIX)
                            ],
                            "providers": tokens["providers"],
                            "model_requests": run.requests,
                            "tokens_in": sum(r.tokens_in for r in sent),
                            "tokens_out": sum(r.tokens_out for r in sent),
                            "queries": run.queries,
                            "tools_sent_per_request": [len(r.tools) for r in sent],
                            "schema_chars_per_request": [r.schema_chars for r in sent],
                            "instruction_chars_per_request": [
                                r.instruction_chars for r in sent
                            ],
                            "first_request_tools": sent[0].tools if sent else [],
                            "focus_spans": _focus_changes(sink, run.run_id),
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
