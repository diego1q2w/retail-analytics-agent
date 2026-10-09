"""Run the conversational efficiency suite (T39-F3) with the real model.

Composition only; scoring lives in ``application.evaluation.efficiency``. Same
harness as ``run_real_model.py``: the ``agent_runtime`` target on the default
LOCAL backend (real investigation runtime, tools, compiler, privacy boundary,
evidence store, output gate and reports) over the offline DuckDB warehouse of
the frozen extract, with the configured live provider chain. Expected figures
come from the extract's independent reference values (``expected.json``), never
from the agent.

The suite (``efficiency/suite.json``) fixes scenarios, repetitions, targets and
a spend ceiling before any run. The runner refuses to start a conversation
whose worst case (per-run budget limits) would cross the ceiling.

Writes ``<out>/<label>.json`` (identifiers, counts, tokens, SQL of the
pseudonymized evaluation data, no answer text), ``<out>/<label>.md`` (every
repetition plus aggregates; with ``--baseline`` a comparison) and
``<out>/transcripts/<label>.md`` (released answers, clarification questions,
reports and SQL, refused if it looks like PII or a secret). Run from the
repository root that holds the ``.env`` (see ``README.md`` here).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from retail_analytics.adapters.evaluation.telemetry_recorder import (
    RecordingTelemetrySink,
)
from retail_analytics.adapters.models.gemini_interactions import PROVIDER
from retail_analytics.application.contracts.evaluation import (
    EfficiencyRun,
    EfficiencyScenario,
    EfficiencySuite,
    ObservedTable,
    QueryRecord,
    RepetitionResult,
    ScenarioInput,
    ScopeSpec,
    Turn,
    TurnResult,
)
from retail_analytics.application.contracts.progress import EventKind
from retail_analytics.application.evaluation.agent_observation import observe, scalar
from retail_analytics.application.evaluation.efficiency import (
    SCORING_VERSION,
    RunFacts,
    attempted_query,
    query_outcome,
    render_summary,
    rescore,
    resolve_figures,
    score_turn,
    spend_allows,
    spend_used,
    worst_case,
)
from retail_analytics.application.evaluation.results import assert_no_sensitive
from retail_analytics.application.query_execution import is_compiler_rejection
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.bootstrap.agent_evaluation import (
    AgentRuntimeTarget,
    _Loop,
    realdata_source,
)
from retail_analytics.bootstrap.budgets import run_limits
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.bootstrap.realdata_benchmark import verify
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.evidence import EvidenceKind
from retail_analytics.domain.runs import ExecutionBackend, RunStatus

HERE = Path(__file__).resolve().parent
EVALUATION = HERE.parent
SUITE = HERE / "efficiency" / "suite.json"
RESULTS = HERE / "efficiency" / "results"
QUERY_CAPABILITY = "execute_analysis"
SAFETY_FLAGS = (
    "pii_released",
    "raw_customer_id_released",
    "out_of_scope_data_released",
    "raw_table_query_executed",
)


@dataclass
class _Conversation:
    session_id: str = ""
    executive_id: str = ""
    facts: list[RunFacts] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    report_texts: tuple[str, ...] = ()
    flags: dict[str, bool] = field(default_factory=dict)


@dataclass
class EfficiencyTarget(AgentRuntimeTarget):
    """The agent_runtime target, driven one request (run) per suite turn."""

    _questions: list[str] = field(default_factory=list, init=False)
    # The run's telemetry recorder: compiler spans give each attempt's SQL.
    recorder: RecordingTelemetrySink | None = field(default=None, init=False)

    def converse(
        self, scenario: EfficiencyScenario, scope: ScopeSpec, case_id: str
    ) -> _Conversation:
        if self._loop is None:
            self._loop = _Loop()
        budget = self.turn_timeout * (len(scenario.turns) * 2 + 1) + 120
        return self._loop.run(self._converse(scenario, scope, case_id), budget)

    async def _converse(
        self, scenario: EfficiencyScenario, scope: ScopeSpec, case_id: str
    ) -> _Conversation:
        harness = await self._start()
        db = harness.persistence
        case = ScenarioInput(
            scenario_id=case_id,
            mode="fixture",
            fixture_ref=None,
            scope=scope,
            dialogue=tuple(Turn(text=t.text) for t in scenario.turns),
        )
        products = self.source.products(scope.product_scope)
        principal, session_id = await self.provision(case, products)
        control = harness.services.control
        out = _Conversation(session_id=session_id, executive_id=principal.executive_id)
        run_ids: list[str] = []
        for turn in scenario.turns:
            handle = await control.start(
                principal,
                session_id=session_id,
                text=turn.text,
                submission_key=uuid.uuid4().hex,
            )
            run_id = handle.run_id
            run_ids.append(run_id)
            await self._wait(db, run_id)
            run = await db.runs.get_run(run_id)
            if run is not None and run.status is RunStatus.WAITING_FOR_INPUT:
                question = await harness.services.inputs.open_question(run_id)
                if question is not None and turn.clarification_reply:
                    await control.answer(
                        principal,
                        run_id=run_id,
                        question_id=question.question_id,
                        text=turn.clarification_reply,
                        submission_key=uuid.uuid4().hex,
                    )
                    await self._wait(db, run_id, after_answer=True)
                else:  # an unexpected question ends the turn unanswered
                    await control.cancel(principal, run_id=run_id)
                    await self._wait_terminal(db, run_id)
        record = await self._record(harness, principal, session_id, run_ids)
        out.report_texts = record.report_texts
        observed = observe(record, self.source.canaries(products))
        out.flags = {
            f: bool(observed.values[f]) for f in SAFETY_FLAGS if f in observed.values
        }
        out.facts = await self._facts(harness, principal, session_id, run_ids, record)
        out.questions = self._questions
        return out

    async def _wait_terminal(self, db: Any, run_id: str) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            run = await db.runs.get_run(run_id)
            if run is not None and run.status.is_terminal:
                return
            await asyncio.sleep(0.2)

    async def _facts(
        self,
        harness: Any,
        principal: Any,
        session_id: str,
        run_ids: Sequence[str],
        record: Any,
    ) -> list[RunFacts]:
        db = harness.persistence
        now = datetime.now(UTC)
        messages = await db.sessions.recent_messages(session_id, 500)
        context = await harness.access.resolver.context_for_run(principal, run_ids[-1])
        session = await harness.evidence.session_standing(context, run_ids=run_ids)
        evidence = {s.evidence.evidence_id: s.evidence for s in session.standings}
        self._questions = []
        facts = []
        for index, run_id in enumerate(run_ids):
            run = await db.runs.get_run(run_id)
            events = list(await db.run_events.replay(run_id, limit=10_000))
            tools = tuple(
                e.tool.capability
                for e in events
                if e.tool is not None and e.kind is EventKind.TOOL_STARTED
            )
            asked = [e for e in events if e.kind is EventKind.INPUT_REQUIRED]
            for event in asked:
                if event.input_request is not None:
                    self._questions.append(event.input_request.question)
            before: int | None = None
            if asked:
                first = asked[0].sequence
                before = sum(
                    1
                    for e in events
                    if e.kind is EventKind.TOOL_SUCCEEDED
                    and e.tool is not None
                    and e.tool.capability == QUERY_CAPABILITY
                    and e.sequence < first
                )
            produced = {
                e.operation_id: e
                for e in evidence.values()
                if e.run_id == run_id and e.content.kind is EvidenceKind.QUERY
            }
            queries = await self._queries(db, run_id, events, produced)
            linked = session.run_links.get(run_id, frozenset())
            tables = tuple(
                ObservedTable(
                    evidence_id=e.evidence_id,
                    columns=e.content.table.column_names,
                    roles=tuple(c.role for c in e.content.table.columns),
                    sources=tuple(c.sources for c in e.content.table.columns),
                    rows=tuple(
                        tuple(scalar(cell) for cell in row)
                        for row in e.content.table.rows
                    ),
                    truncated=e.content.table.truncated,
                    scope_matches=True,
                )
                for eid in linked
                if (e := evidence.get(eid)) is not None
            )
            budget = await db.budgets.get(run_id)
            usage = budget.usage if budget is not None else None
            ended = (run.completed_at or run.updated_at) if run else now
            released = "\n".join(
                m.content
                for m in messages
                if m.role is MessageRole.ASSISTANT and m.run_id == run_id
            )
            last = index == len(run_ids) - 1
            facts.append(
                RunFacts(
                    run_id=run_id,
                    run_status=run.status.value if run else "missing",
                    tools=tools,
                    queries=tuple(queries),
                    queries_before_question=before,
                    asked_clarification=bool(asked),
                    budget_tokens=usage.tokens if usage else 0,
                    active_seconds=usage.active_seconds(now) if usage else 0.0,
                    wall_seconds=(ended - run.created_at).total_seconds()
                    if run
                    else 0.0,
                    released_text=released
                    + ("\n" + "\n".join(record.report_texts) if last else ""),
                    tables=tables,
                    session_evidence_ids=frozenset(evidence),
                    report_saved=bool(record.report_texts) if last else None,
                    report_actions=record.report_actions if last else None,
                )
            )
        return facts

    async def _queries(
        self,
        db: Any,
        run_id: str,
        events: Sequence[Any],
        produced: dict[str, Any],
    ) -> list[QueryRecord]:
        """Every ``execute_analysis`` call of the run, in call order: its
        durable operation (if one was created), whether a warehouse job was
        registered, what the tool returned and, without evidence, the SQL the
        model wrote (sanitized compiler-span capture)."""
        operations = {
            op.operation_id: op
            for op in await db.tool_executions.for_run(run_id)
            if op.capability == QUERY_CAPABILITY
        }
        results: dict[str, tuple[str, str | None]] = {}
        for e in events:
            operation_id = e.correlation.operation_id
            if (
                e.tool is None
                or e.tool.capability != QUERY_CAPABILITY
                or operation_id is None
                or e.kind is EventKind.TOOL_STARTED
            ):
                continue
            code = e.tool.error_code.value if e.tool.error_code else None
            results[operation_id] = (e.kind.value.removeprefix("tool."), code)
        order = list(dict.fromkeys([*results, *operations]))
        spans = self.recorder.spans() if self.recorder is not None else ()
        records = []
        for operation_id in order:
            op = operations.get(operation_id)
            tool_result, tool_code = results.get(operation_id, (None, None))
            executed = await db.query_jobs.get_job(operation_id) is not None
            code = op.error_code.value if op and op.error_code else tool_code
            ev = produced.get(operation_id)
            attempted, attempted_parameters = (
                (None, {}) if ev else attempted_query(spans, operation_id)
            )
            records.append(
                QueryRecord(
                    outcome=query_outcome(
                        status=op.status.value if op else None,
                        compiler_rejected=op is not None and is_compiler_rejection(op),
                        executed=executed,
                        error_code=code,
                    ),
                    error_code=code,
                    evidence_id=ev.evidence_id if ev else None,
                    rows=len(ev.content.table.rows) if ev else None,
                    sql=ev.content.provenance.logical_sql if ev else None,
                    parameters={
                        p.name: str(p.value) for p in ev.content.provenance.parameters
                    }
                    if ev
                    else {},
                    executed=executed,
                    reason=op.error_detail if op else None,
                    tool_result=tool_result,
                    attempted_sql=attempted,
                    attempted_parameters=attempted_parameters,
                )
            )
        return records


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(HERE), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def code_revision() -> str:
    """Short commit, marked ``+dirty`` when ``src`` has uncommitted changes."""
    with contextlib.suppress(Exception):
        dirty = "+dirty" if _git("status", "--porcelain", "--", "src") else ""
        return _git("rev-parse", "--short", "HEAD") + dirty
    return "unknown"


def _transcript(
    scenario: EfficiencyScenario, rep: RepetitionResult, conversation: _Conversation
) -> list[str]:
    lines = [f"## {scenario.id} (repetition {rep.repetition})", ""]
    for turn_spec, facts, turn in zip(
        scenario.turns, conversation.facts, rep.turns, strict=False
    ):
        lines += [f"### Turn {turn.turn}: `{turn.run_id}` ({turn.run_status})", ""]
        lines += [f"**User:** {turn_spec.text}", ""]
        if turn.asked_clarification:
            for question in conversation.questions:
                lines += [f"**Clarification asked:** {question}", ""]
            if turn_spec.clarification_reply:
                lines += [f"**User reply:** {turn_spec.clarification_reply}", ""]
        for number, query in enumerate(turn.queries, 1):
            lines += [
                f"Query {number}: {query.outcome}"
                + (f" ({query.error_code})" if query.error_code else "")
                + (f", {query.rows} rows" if query.rows is not None else ""),
                "",
            ]
            if query.sql:
                lines += ["```sql", query.sql.strip(), "```", ""]
            elif query.attempted_sql:
                lines += [
                    f"Attempted SQL ({query.reason or 'no evidence'}):",
                    "",
                    "```sql",
                    query.attempted_sql.strip(),
                    "```",
                    "",
                ]
            if query.parameters:
                bound = ", ".join(f"{k}={v}" for k, v in query.parameters.items())
                lines += [f"Parameters: {bound}", ""]
        lines += ["**Released:**", "", facts.released_text.strip() or "_nothing_", ""]
    return [line.rstrip() for line in lines]


def _rescore(out: Path, label: str, suite_path: Path, baseline: Path | None) -> int:
    suite = EfficiencySuite.model_validate_json(suite_path.read_text("utf-8"))
    path = out / f"{label}.json"
    run = rescore(EfficiencyRun.model_validate_json(path.read_text("utf-8")), suite)
    _write(out, run, baseline)
    return 0


def _write(out: Path, run: EfficiencyRun, baseline_path: Path | None) -> None:
    text = json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True)
    assert_no_sensitive(text)
    (out / f"{run.label}.json").write_text(text + "\n", encoding="utf-8")
    baseline = (
        EfficiencyRun.model_validate_json(baseline_path.read_text("utf-8"))
        if baseline_path
        else None
    )
    summary = render_summary(run, baseline)
    (out / f"{run.label}.md").write_text(summary, encoding="utf-8")
    print(summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", required=True, help="e.g. baseline, candidate")
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--only", nargs="*", help="scenario IDs (default: all)")
    parser.add_argument("--baseline", type=Path, help="results JSON to compare")
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    # Harness check only: results with a cap are not suite results.
    parser.add_argument("--max-repeats", type=int, default=0)
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="recompute targets of <out>/<label>.json; runs nothing",
    )
    args = parser.parse_args(argv)
    if args.rescore:
        return _rescore(args.out, args.label, args.suite, args.baseline)

    problems = verify(EVALUATION / "realdata")
    if problems:
        print("frozen extract check failed:", *problems, sep="\n  ")
        return 3
    settings = load_backend_settings()
    if settings.gemini_api_key is None:
        print("blocked: no Gemini key configured; nothing was run")
        return 3
    suite = EfficiencySuite.model_validate_json(args.suite.read_text("utf-8"))
    limits = run_limits(settings)
    if (
        limits.tokens > suite.spend.run_tokens_limit
        or limits.provider_requests > suite.spend.run_requests_limit
    ):
        print("blocked: run budgets exceed the suite's worst-case assumption")
        return 3
    print("worst case:", worst_case(suite), "ceiling:", suite.spend.model_dump())
    spec = json.loads((EVALUATION / "realdata/spec.json").read_text("utf-8"))
    expected_doc = json.loads(
        (EVALUATION / "realdata/expected.json").read_text("utf-8")
    )
    expected = {q: v["values"] for q, v in expected_doc["queries"].items()}
    tolerances = {
        q: {o: float(d.get("tol", 0.0)) for o, d in v["outputs"].items()}
        for q, v in spec["queries"].items()
    }
    revision = code_revision()
    scenarios = [s for s in suite.scenarios if not args.only or s.id in args.only]
    sink = RecordingTelemetrySink()
    repetitions: list[RepetitionResult] = []
    transcript: list[str] = [f"# Transcripts: {args.label} (code `{revision}`)", ""]
    stopped = False
    target = EfficiencyTarget(
        settings,
        realdata_source(EVALUATION),
        provider_chain(settings),
        turn_timeout=args.turn_timeout,
        backend=ExecutionBackend.LOCAL,
    )
    target.recorder = sink
    with use_telemetry(Telemetry(sink)):
        try:
            for scenario in scenarios:
                figures = [
                    resolve_figures(t, expected, tolerances) for t in scenario.turns
                ]
                scope_spec = spec["scopes"][scenario.scope]
                scope = ScopeSpec(
                    executive_ref=scope_spec["executive_ref"],
                    product_scope=(
                        f"products:{scope_spec['product_lo']}-"
                        f"{scope_spec['product_hi']}",
                    ),
                )
                repeats = scenario.repeats
                if args.max_repeats:
                    repeats = min(repeats, args.max_repeats)
                for rep in range(1, repeats + 1):
                    used = spend_used(repetitions)
                    if not spend_allows(used, suite.spend, len(scenario.turns)):
                        print(f"spend ceiling reached before {scenario.id} #{rep}")
                        stopped = True
                        break
                    started = datetime.now(UTC).isoformat(timespec="seconds")
                    print(f"running {scenario.id} #{rep} ...", flush=True)
                    case_id = f"efficiency-{scenario.id}"
                    try:
                        conversation = target.converse(scenario, scope, case_id)
                    except Exception as error:  # report every repetition
                        repetitions.append(
                            RepetitionResult(
                                scenario_id=scenario.id,
                                repetition=rep,
                                session_id="-",
                                executive_id="-",
                                started_at=started,
                                code_revision=revision,
                                error=type(error).__name__,
                            )
                        )
                        print(f"  error {type(error).__name__}", flush=True)
                        continue
                    spans = sink.spans()
                    turns: list[TurnResult] = [
                        score_turn(i, scenario.turns[i - 1], figures[i - 1], f, spans)
                        for i, f in enumerate(conversation.facts, 1)
                    ]
                    result = RepetitionResult(
                        scenario_id=scenario.id,
                        repetition=rep,
                        session_id=conversation.session_id,
                        executive_id=conversation.executive_id,
                        started_at=started,
                        code_revision=revision,
                        turns=tuple(turns),
                        safety_flags=conversation.flags,
                        transcript=f"transcripts/{args.label}.md",
                    )
                    repetitions.append(result)
                    transcript += _transcript(scenario, result, conversation)
                    for t in turns:
                        print(
                            f"  turn {t.turn} {t.run_status} q={t.queries_succeeded}"
                            f"/{t.queries_rejected}r/{t.queries_failed}f "
                            f"req={t.model_requests} "
                            f"tok={t.input_tokens}+{t.output_tokens} "
                            f"targets={dict(t.targets_met)} "
                            f"fig={[f.in_answer for f in t.figures]}",
                            flush=True,
                        )
                if stopped:
                    break
        finally:
            target.close()
    extract = json.loads(
        (EVALUATION / "realdata/extract/extract-manifest.json").read_text("utf-8")
    )
    run = EfficiencyRun(
        label=args.label,
        suite_id=suite.suite_id,
        suite_version=suite.suite_version,
        scoring_version=SCORING_VERSION,
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        code_revision=revision,
        target_id=target.target_id,
        execution_backend=ExecutionBackend.LOCAL.value,
        warehouse="offline DuckDB (frozen extract)",
        data_ref=str(extract["data_ref"]),
        extract_digest=str(extract["extract_digest"]),
        primary_provider=PROVIDER,
        configured_models={
            PROVIDER: settings.agent_gemini_model,
            **(
                {"openai": settings.agent_openai_model}
                if settings.openai_api_key is not None
                else {}
            ),
        },
        settings={
            "turn_timeout_seconds": args.turn_timeout,
            "max_repeats_cap": args.max_repeats,
            **{f"run_limit_{k}": v for k, v in limits.as_dict().items()},
        },
        spend=suite.spend,
        spend_used=spend_used(repetitions),
        stopped_by_ceiling=stopped,
        repetitions=tuple(repetitions),
    )
    out: Path = args.out
    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    # Trailing spaces (Markdown hard breaks) are dropped to keep diffs clean.
    transcript_text = (
        "\n".join(line.rstrip() for line in "\n".join(transcript).splitlines()).rstrip()
        + "\n"
    )
    assert_no_sensitive(transcript_text)
    (out / "transcripts" / f"{args.label}.md").write_text(
        transcript_text, encoding="utf-8"
    )
    _write(out, run, args.baseline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
