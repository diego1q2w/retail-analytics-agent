"""Conversational efficiency suite (T39-F3): scoring, spend and comparison.

Pure functions over what one real-model run left behind (telemetry spans,
durable run records, released text and the logical SQL of its evidence):

- :func:`resolve_figures` reads the independent reference values (frozen
  extract, two SQL routes) for a turn; nothing is hardcoded;
- :func:`attempts_for_run` / :func:`restarts_for_run` summarize model attempts
  (provider, outcome, tokens, fallback) and context restarts per run;
- :func:`score_turn` checks figures in evidence and in the released text,
  required period/definition words, required SQL fragments, citations and the
  declared query/model-request targets; tools the turn must (not) call, the
  period every query keeps and claims that must be labelled as hypotheses
  (:func:`unqualified_terms`);
- :func:`spend_allows` enforces the spend ceiling declared before the run;
- :func:`render_summary` writes every repetition and per-turn aggregates,
  optionally next to an explicitly identified baseline.

Targets are evaluation targets fixed before the run, never production caps.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from retail_analytics.application.contracts.evaluation import (
    EfficiencyRun,
    EfficiencySuite,
    EfficiencyTurn,
    FigureCheck,
    ModelAttemptRecord,
    ObservedTable,
    QueryRecord,
    RecordedSpan,
    RepetitionResult,
    SpendCeiling,
    TurnResult,
)
from retail_analytics.application.contracts.telemetry import Span
from retail_analytics.application.evaluation.real_model import (
    fold_label,
    number_in_evidence,
    number_in_text,
)

_CITATION = re.compile(r"\bevd_[0-9a-f]{8,64}\b")
_SPACE = re.compile(r"\s+")


# Bumped when target scoring changes; ``rescore`` brings saved runs up to date.
SCORING_VERSION = 4


class SuiteError(ValueError):
    """The suite refers to a reference value that does not exist."""


@dataclass(frozen=True, slots=True)
class ResolvedFigure:
    name: str
    kind: str
    value: float | str
    tol: float


def resolve_figures(
    turn: EfficiencyTurn,
    expected: Mapping[str, Mapping[str, object]],
    tolerances: Mapping[str, Mapping[str, float]] | None = None,
) -> tuple[ResolvedFigure, ...]:
    """Expected figures of ``turn`` from ``expected[query][output]``."""

    def value(ref: str) -> object:
        query, _, output = ref.partition(".")
        try:
            return expected[query][output]
        except KeyError:
            raise SuiteError(f"unknown reference value {ref!r}") from None

    def tol(ref: str) -> float:
        query, _, output = ref.partition(".")
        return float((tolerances or {}).get(query, {}).get(output, 0.01))

    resolved: list[ResolvedFigure] = []
    for figure in turn.figures:
        raw = value(figure.ref)
        if figure.kind == "label":
            resolved.append(ResolvedFigure(figure.name, "label", str(raw), 0.0))
            continue
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise SuiteError(f"{figure.ref!r} is not a number")
        number = float(raw)
        tolerance = tol(figure.ref)
        if figure.minus is not None:
            other = value(figure.minus)
            if isinstance(other, bool) or not isinstance(other, int | float):
                raise SuiteError(f"{figure.minus!r} is not a number")
            number = round(number - float(other), 2)
            tolerance += tol(figure.minus)
        resolved.append(ResolvedFigure(figure.name, "number", number, tolerance))
    return tuple(resolved)


def attempts_for_run(
    spans: Iterable[RecordedSpan], run_id: str
) -> tuple[ModelAttemptRecord, ...]:
    """Every model attempt of the run, failed ones included, in span order."""
    found = []
    for span in spans:
        if span.run_id != run_id or span.name != Span.MODEL_ATTEMPT.value:
            continue
        attrs = span.attributes
        found.append(
            ModelAttemptRecord(
                provider=str(attrs.get("provider", "unknown")),
                model=str(attrs.get("model", "unknown")),
                outcome=str(attrs.get("outcome", "unknown")),
                input_tokens=int(attrs.get("input_tokens", 0) or 0),
                output_tokens=int(attrs.get("output_tokens", 0) or 0),
                fallback_from=str(attrs.get("fallback_from", "none")),
                reason_class=str(attrs.get("reason_class", "none")),
            )
        )
    return tuple(found)


def restarts_for_run(spans: Iterable[RecordedSpan], run_id: str) -> tuple[str, ...]:
    return tuple(
        str(span.attributes.get("restart.cause", "unknown"))
        for span in spans
        if span.run_id == run_id and span.name == Span.CONTEXT_RESTART.value
    )


def span_value(
    spans: Iterable[RecordedSpan], run_id: str, name: str, key: str
) -> str | None:
    """Last recorded ``key`` of the run's ``name`` span, if any."""
    value: str | None = None
    for span in spans:
        if span.run_id == run_id and span.name == name and key in span.attributes:
            value = str(span.attributes[key])
    return value


def _fold(text: str) -> str:
    """Whitespace-, case- and dash-insensitive ("65\u201369" is "65-69")."""
    return fold_label(_SPACE.sub(" ", text))


def _label_in_tables(label: str, tables: Iterable[ObservedTable]) -> bool:
    folded = _fold(label)
    return any(
        isinstance(cell, str) and _fold(cell) == folded
        for table in tables
        for row in table.rows
        for cell in row
    )


def figure_results(
    figures: Sequence[ResolvedFigure], text: str, tables: Sequence[ObservedTable]
) -> tuple[FigureCheck, ...]:
    """Each figure in any evidence cell of the turn, and in its released text."""
    checks = []
    for figure in figures:
        if figure.kind == "label":
            checks.append(
                FigureCheck(
                    name=figure.name,
                    kind="label",
                    in_evidence=_label_in_tables(str(figure.value), tables),
                    in_answer=_fold(str(figure.value)) in _fold(text),
                )
            )
            continue
        expected = float(figure.value)
        share = abs(expected) < 1 and "share" in figure.name
        tol = max(figure.tol, 0.005)
        checks.append(
            FigureCheck(
                name=figure.name,
                kind="number",
                expected=expected,
                in_evidence=number_in_evidence(expected, tol, tables, share=share),
                in_answer=number_in_text(expected, tol, text, share=share),
            )
        )
    return tuple(checks)


def terms_met(
    groups: Sequence[Sequence[str]], texts: Iterable[str]
) -> tuple[bool, ...]:
    """Per group: does any of its fragments occur in any of ``texts``?"""
    folded = [_fold(t) for t in texts]
    return tuple(
        any(_fold(fragment) in text for fragment in group for text in folded)
        for group in groups
    )


# Wording that marks a statement as untested, conditional or a limitation.
_QUALIFIERS = (
    "hypothes",
    "not tested",
    "untested",
    "not measured",
    "unmeasured",
    "not verified",
    "unverified",
    "not established",
    "not confirmed",
    "cannot",
    "can't",
    "could not",
    "not show",
    "doesn't show",
    "does not prove",
    "not proof",
    "no evidence",
    "no data",
    "not in the data",
    "not available",
    "not necessarily",
    "not the same as",
    "rather than",
    "may ",
    "might",
    "could",
    "possibl",
    "speculat",
    "assum",
    "would need",
    "to test",
    "whether",
    "not establish",
    # Negations: "buyers, not website visitors", "never", "neither ... nor".
    " not ",
    "n't ",
    "never",
    "neither",
    " nor ",
)
_HEADING = re.compile(r"^\s*(#+\s|\*\*[^*]+\*\*:?\s*$|[^.!?]{1,80}:\s*$)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# Recommended actions: a heading over them, or a label leading the line.
_PROPOSAL_WORDS = (
    r"recommend\w*|suggested actions?|proposed actions?|action items?|next steps?"
)
_PROPOSAL_HEADING = re.compile(_PROPOSAL_WORDS)
_PROPOSAL_LABEL = re.compile(rf"^[\W\d]*(?:{_PROPOSAL_WORDS})[*_ ]*:")
# Levers the business itself pulls: a proposal may name one ("review
# promotional support") without claiming it caused anything.
_LEVERS = ("marketing", "campaign", "promotion", "promotional", "advertis")
# Wording that attributes a cause, which a proposal may not do unlabelled.
_CAUSAL = (
    "because",
    "due to",
    "driven by",
    "drove",
    "caused",
    "led to",
    "result of",
    "resulted",
    "thanks to",
    "explain",
    "attributable",
)


def _qualified(text: str) -> bool:
    return any(q in text for q in _QUALIFIERS)


def _lever(term: str) -> bool:
    return _fold(term).startswith(_LEVERS)


def unqualified_terms(terms: Sequence[str], text: str) -> tuple[str, ...]:
    """Terms the text states without labelling them as untested.

    Each sentence that names a term must itself carry qualifying wording
    (hypothesis, not tested, may, cannot show ...) or sit under a heading
    that does, so a labelled "Hypotheses" section passes and an unlabelled
    heading such as "Seasonal demand drove growth" fails.

    Recommended actions (under a recommendations heading or led by a
    "Recommendation:" label) are proposals, not findings: naming a lever the
    business pulls (marketing, campaign, promotion) there is not a causal
    claim, unless the sentence attributes a cause ("because the campaign
    drove growth"). Conditions such as traffic, seasonality or new customers
    are still claims there (scoring v4; v3 flagged every lever too).
    """
    missed: dict[str, None] = {}
    heading_qualified = False
    in_proposals = False
    for raw in text.splitlines():
        line = _fold(raw)
        if not line.strip():
            continue
        if _HEADING.match(line):
            heading_qualified = _qualified(line)
            in_proposals = bool(_PROPOSAL_HEADING.search(line))
            sentences = [line]
        else:
            sentences = _SENTENCE.split(line)
        proposal = in_proposals or bool(_PROPOSAL_LABEL.match(line))
        for sentence in sentences:
            if heading_qualified or _qualified(sentence):
                continue
            causal = any(c in sentence for c in _CAUSAL)
            for term in terms:
                if _fold(term) not in sentence:
                    continue
                if proposal and _lever(term) and not causal:
                    continue
                missed[term] = None
    return tuple(missed)


def unscoped_queries(groups: Sequence[Sequence[str]], sqls: Sequence[str]) -> int:
    """Executed queries that miss any of ``groups`` (e.g. the period)."""
    if not groups:
        return 0
    return sum(not all(terms_met(groups, [sql])) for sql in sqls)


def citations(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_CITATION.findall(text)))


@dataclass(frozen=True, slots=True)
class RunFacts:
    """What the harness collected for one run (one user request)."""

    run_id: str
    run_status: str
    tools: tuple[str, ...]
    queries: tuple[QueryRecord, ...]
    queries_before_question: int | None
    asked_clarification: bool
    budget_tokens: int
    active_seconds: float
    wall_seconds: float
    released_text: str
    tables: tuple[ObservedTable, ...]
    session_evidence_ids: frozenset[str]
    report_saved: bool | None = None
    report_actions: int | None = None


# Refused before reaching the warehouse: the tool's input validation.
_INPUT_REJECTION = "INVALID_INPUT"


def query_outcome(
    *,
    status: str | None,
    compiler_rejected: bool,
    executed: bool,
    error_code: str | None,
) -> str:
    """Outcome of one ``execute_analysis`` attempt for the record.

    ``status`` is the durable operation status (None when the call was
    refused before an operation existed). A compiler or input rejection is
    ``rejected``: it cost a model turn but never ran on the warehouse.
    """
    if compiler_rejected and not executed:
        return "rejected"
    if status is None:
        return "rejected" if error_code == _INPUT_REJECTION else "failed"
    return status


def attempted_query(
    spans: Sequence[RecordedSpan], operation_id: str
) -> tuple[str | None, dict[str, str]]:
    """The SQL and parameters the model sent for ``operation_id``, from the
    last compiler span that captured them (sanitized content)."""
    for span in reversed(spans):
        if (
            span.name != Span.COMPILE.value
            or span.attributes.get("operation_id") != operation_id
        ):
            continue
        inputs = span.content.get("inputs")
        if isinstance(inputs, Mapping):
            sql = inputs.get("generated_sql")
            raw = inputs.get("parameters")
            parameters = (
                {str(k): str(v) for k, v in raw.items()}
                if isinstance(raw, Mapping)
                else {}
            )
            return (sql if isinstance(sql, str) else None), parameters
    return None, {}


def turn_targets(
    turn: EfficiencyTurn,
    *,
    run_status: str,
    queries: int,
    model_requests: int,
    queries_before_question: int | None,
    asked_clarification: bool,
    report_saved: bool | None,
    report_actions: int | None,
    tools: Sequence[str] = (),
    unscoped: int = 0,
    unqualified: Sequence[str] = (),
) -> dict[str, bool]:
    """Declared targets of the turn, plus two that every turn has: the run
    completed (a partial, cancelled or failed run has not answered) and it
    asked no clarification it was not expected to ask."""
    targets = {"completed": run_status == "completed"}
    if turn.expect_clarification:
        targets["asked_clarification"] = asked_clarification
    else:
        targets["no_unexpected_question"] = not asked_clarification
    if turn.targets.max_queries is not None:
        targets["queries"] = queries <= turn.targets.max_queries
    if turn.targets.max_model_requests is not None:
        targets["model_requests"] = model_requests <= turn.targets.max_model_requests
    if turn.targets.max_queries_before_question is not None:
        targets["queries_before_question"] = (
            queries_before_question is not None
            and queries_before_question <= turn.targets.max_queries_before_question
        )
    if turn.expect_report:
        targets["report_with_actions"] = bool(report_saved) and bool(report_actions)
    if turn.forbidden_tools:
        targets["no_forbidden_tools"] = not set(turn.forbidden_tools) & set(tools)
    if turn.required_tools:
        targets["required_tools"] = all(
            set(group) & set(tools) for group in turn.required_tools
        )
    if turn.every_sql_terms:
        targets["every_query_scoped"] = unscoped == 0
    if turn.qualified_terms:
        targets["claims_qualified"] = not unqualified
    return targets


def rescore(
    run: EfficiencyRun,
    suite: EfficiencySuite,
    released: Mapping[str, str] | None = None,
) -> EfficiencyRun:
    """Recompute targets from a saved run (scoring changes, no new model run).

    ``released`` maps run IDs to the released text recorded in the run's
    transcript; with it, text checks (``unqualified_terms``) are recomputed
    too, otherwise their saved results are kept.
    """
    specs = {s.id: s for s in suite.scenarios}
    texts = released or {}
    repetitions = []
    for rep in run.repetitions:
        scenario = specs[rep.scenario_id]
        turns = []
        for t in rep.turns:
            spec = scenario.turns[t.turn - 1]
            unqualified = (
                unqualified_terms(spec.qualified_terms, texts[t.run_id])
                if t.run_id in texts
                else t.unqualified_terms
            )
            targets = turn_targets(
                spec,
                run_status=t.run_status,
                queries=t.queries_succeeded,
                model_requests=t.model_requests,
                queries_before_question=t.queries_before_question,
                asked_clarification=t.asked_clarification,
                report_saved=t.report_saved,
                report_actions=t.report_actions,
                tools=t.tools,
                unscoped=t.unscoped_queries,
                unqualified=unqualified,
            )
            turns.append(
                t.model_copy(
                    update={"targets_met": targets, "unqualified_terms": unqualified}
                )
            )
        repetitions.append(rep.model_copy(update={"turns": tuple(turns)}))
    return run.model_copy(
        update={"repetitions": tuple(repetitions), "scoring_version": SCORING_VERSION}
    )


_TURN_HEADING = re.compile(r"^### Turn \d+: `([^`]+)`")
_REPETITION_HEADING = re.compile(r"^## \S+ \(repetition \d+\)$")


def released_texts(transcript: str) -> dict[str, str]:
    """Run ID -> released text, from a transcript the harness wrote."""
    found: dict[str, str] = {}
    run_id: str | None = None
    lines: list[str] | None = None
    for line in transcript.splitlines():
        heading = _TURN_HEADING.match(line)
        if heading or _REPETITION_HEADING.match(line):
            if run_id is not None and lines is not None:
                found[run_id] = "\n".join(lines).strip()
            run_id = heading.group(1) if heading else None
            lines = None
        elif line == "**Released:**" and run_id is not None:
            lines = []
        elif lines is not None:
            lines.append(line)
    if run_id is not None and lines is not None:
        found[run_id] = "\n".join(lines).strip()
    return found


def score_turn(
    index: int,
    turn: EfficiencyTurn,
    figures: Sequence[ResolvedFigure],
    facts: RunFacts,
    spans: Sequence[RecordedSpan],
) -> TurnResult:
    attempts = attempts_for_run(spans, facts.run_id)
    succeeded = [a for a in attempts if a.outcome == "succeeded"]
    input_tokens = sum(a.input_tokens for a in succeeded)
    output_tokens = sum(a.output_tokens for a in succeeded)
    queries_ok = sum(q.outcome == "succeeded" for q in facts.queries)
    cited = citations(facts.released_text)
    # Only evidence SQL meets SQL terms (``attempted_sql`` never does).
    sqls = [
        q.sql + " " + " ".join(f"{k}={v}" for k, v in q.parameters.items())
        for q in facts.queries
        if q.sql
    ]
    executed = [
        q.sql + " " + " ".join(f"{k}={v}" for k, v in q.parameters.items())
        for q in facts.queries
        if q.sql and q.outcome == "succeeded"
    ]
    unscoped = unscoped_queries(turn.every_sql_terms, executed)
    unqualified = unqualified_terms(turn.qualified_terms, facts.released_text)
    targets = turn_targets(
        turn,
        run_status=facts.run_status,
        queries=queries_ok,
        model_requests=len(attempts),
        queries_before_question=facts.queries_before_question,
        asked_clarification=facts.asked_clarification,
        report_saved=facts.report_saved,
        report_actions=facts.report_actions,
        tools=facts.tools,
        unscoped=unscoped,
        unqualified=unqualified,
    )
    extra = (
        max(queries_ok - turn.targets.max_queries, 0)
        if turn.targets.max_queries is not None
        else 0
    )
    return TurnResult(
        turn=index,
        kind=turn.kind,
        run_id=facts.run_id,
        run_status=facts.run_status,
        tools=facts.tools,
        queries_succeeded=queries_ok,
        queries_failed=sum(
            q.outcome not in ("succeeded", "rejected") for q in facts.queries
        ),
        queries_rejected=sum(q.outcome == "rejected" for q in facts.queries),
        queries=facts.queries,
        queries_before_question=facts.queries_before_question,
        asked_clarification=facts.asked_clarification,
        attempts=attempts,
        model_requests=len(attempts),
        model_requests_failed=len(attempts) - len(succeeded),
        fallbacks=sum(a.fallback_from != "none" for a in attempts),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        budget_tokens=facts.budget_tokens,
        active_seconds=round(facts.active_seconds, 1),
        wall_seconds=round(facts.wall_seconds, 1),
        restarts=restarts_for_run(spans, facts.run_id),
        admission=span_value(spans, facts.run_id, Span.ADMISSION.value, "decision"),
        stop_reason=span_value(spans, facts.run_id, Span.RUN.value, "stop_reason"),
        figures=figure_results(figures, facts.released_text, facts.tables),
        text_terms_met=terms_met(turn.text_terms, [facts.released_text]),
        sql_terms_met=terms_met(turn.sql_terms, sqls),
        citations=len(cited),
        cited_unknown=sum(c not in facts.session_evidence_ids for c in cited),
        report_saved=facts.report_saved,
        report_actions=facts.report_actions,
        max_evidence_rows=max((len(t.rows) for t in facts.tables), default=0),
        targets_met=targets,
        extra_queries=extra,
        unscoped_queries=unscoped,
        unqualified_terms=unqualified,
    )


# -- spend --------------------------------------------------------------------


def spend_used(repetitions: Iterable[RepetitionResult]) -> dict[str, int]:
    turns = [t for r in repetitions for t in r.turns]
    return {
        "total_tokens": sum(t.total_tokens for t in turns),
        "model_attempts": sum(t.model_requests for t in turns),
        "runs": len(turns),
    }


def spend_allows(used: Mapping[str, int], ceiling: SpendCeiling, runs: int) -> bool:
    """Whether a conversation of ``runs`` runs fits even in its worst case."""
    return (
        used.get("total_tokens", 0) + runs * ceiling.run_tokens_limit
        <= ceiling.max_total_tokens
        and used.get("model_attempts", 0) + runs * ceiling.run_requests_limit
        <= ceiling.max_model_attempts
    )


def worst_case(suite: EfficiencySuite) -> dict[str, int]:
    runs = sum(len(s.turns) * s.repeats for s in suite.scenarios)
    return {
        "runs": runs,
        "total_tokens": runs * suite.spend.run_tokens_limit,
        "model_attempts": runs * suite.spend.run_requests_limit,
    }


# -- summary ------------------------------------------------------------------


def _stats(values: Sequence[float]) -> str:
    if not values:
        return "-"
    if len(values) == 1:
        return f"{values[0]:,.0f}"
    return f"{statistics.median(values):,.0f} ({min(values):,.0f}-{max(values):,.0f})"


def _figures(turn: TurnResult) -> str:
    numbers = [f for f in turn.figures]
    if not numbers:
        return "-"
    ev = sum(bool(f.in_evidence) for f in numbers)
    stated = sum(f.in_answer for f in numbers)
    return f"{ev}/{len(numbers)} ev, {stated}/{len(numbers)} text"


def _targets(turn: TurnResult) -> str:
    if not turn.targets_met:
        return "-"
    missed = [k for k, ok in turn.targets_met.items() if not ok]
    if turn.unqualified_terms:
        missed = [
            f"{k} ({'/'.join(turn.unqualified_terms)})"
            if k == "claims_qualified"
            else k
            for k in missed
        ]
    return "met" if not missed else "MISSED " + ",".join(missed)


def _grouped(
    run: EfficiencyRun,
) -> dict[tuple[str, int], list[tuple[RepetitionResult, TurnResult]]]:
    grouped: dict[tuple[str, int], list[tuple[RepetitionResult, TurnResult]]] = {}
    for rep in run.repetitions:
        for turn in rep.turns:
            grouped.setdefault((rep.scenario_id, turn.turn), []).append((rep, turn))
    return grouped


@dataclass(frozen=True, slots=True)
class TurnStats:
    """One scenario turn over its repetitions."""

    kind: str
    n: int
    targets_met: int
    figures_ok: int
    queries: tuple[float, ...]
    requests: tuple[float, ...]
    input_tokens: tuple[float, ...]
    total_tokens: tuple[float, ...]
    active_seconds: tuple[float, ...]

    def median(self, metric: str) -> float:
        values: tuple[float, ...] = getattr(self, metric)
        return float(statistics.median(values)) if values else 0.0


def _all_right(turn: TurnResult) -> bool:
    """Every expected figure is stated in the released text. Evidence matches
    are reported per repetition; a reuse turn cites earlier runs' evidence and
    a derived difference has no evidence cell, so they are not required."""
    return bool(turn.figures) and all(f.in_answer for f in turn.figures)


def aggregate(run: EfficiencyRun) -> dict[tuple[str, int], TurnStats]:
    """Per scenario turn: repetitions, target hits and value lists."""
    result: dict[tuple[str, int], TurnStats] = {}
    for key, items in _grouped(run).items():
        turns = [t for _, t in items]
        result[key] = TurnStats(
            kind=turns[0].kind,
            n=len(turns),
            targets_met=sum(
                bool(t.targets_met) and all(t.targets_met.values()) for t in turns
            ),
            figures_ok=sum(_all_right(t) for t in turns),
            queries=tuple(float(t.queries_succeeded) for t in turns),
            requests=tuple(float(t.model_requests) for t in turns),
            input_tokens=tuple(float(t.input_tokens) for t in turns),
            total_tokens=tuple(float(t.total_tokens) for t in turns),
            active_seconds=tuple(t.active_seconds for t in turns),
        )
    return result


def render_summary(run: EfficiencyRun, baseline: EfficiencyRun | None = None) -> str:
    providers = sorted(
        {
            f"{a.provider}:{a.model}"
            for rep in run.repetitions
            for t in rep.turns
            for a in t.attempts
            if a.outcome == "succeeded"
        }
    )
    lines = [
        f"# Conversation efficiency: {run.label}",
        "",
        f"- Recorded {run.recorded_at}; code `{run.code_revision}`; suite "
        f"`{run.suite_id}` v{run.suite_version}; scoring v{run.scoring_version}",
        *(
            [
                f"- Rescored from `{run.rescored_from}` (scoring "
                f"v{run.rescored_from_scoring_version}) with scoring "
                f"v{run.scoring_version}; recorded runs and transcripts "
                "unchanged, no model was run again"
            ]
            if run.rescored_from
            else []
        ),
        f"- Target `{run.target_id}` (backend `{run.execution_backend}`), "
        f"warehouse `{run.warehouse}` (`{run.data_ref}`, digest "
        f"`{run.extract_digest[:12]}`)",
        "- Configured models: "
        + ", ".join(f"{k}={v}" for k, v in sorted(run.configured_models.items())),
        f"- Providers that answered: {', '.join(providers) or 'none'}",
        f"- Spend ceiling: {run.spend.max_total_tokens:,} tokens / "
        f"{run.spend.max_model_attempts} model attempts; used "
        f"{run.spend_used.get('total_tokens', 0):,} tokens / "
        f"{run.spend_used.get('model_attempts', 0)} attempts over "
        f"{run.spend_used.get('runs', 0)} runs"
        + (" (stopped by the ceiling)" if run.stopped_by_ceiling else ""),
        "",
        "## Aggregates per turn (median (min-max) over repetitions)",
        "",
        "| scenario | turn | kind | n | targets met | figures right | queries "
        "| model requests | input tokens | total tokens | active s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    stats = aggregate(run)
    for (scenario, turn), row in stats.items():
        lines.append(
            f"| {scenario} | {turn} | {row.kind} | {row.n} "
            f"| {row.targets_met}/{row.n} | {row.figures_ok}/{row.n} "
            f"| {_stats(row.queries)} | {_stats(row.requests)} "
            f"| {_stats(row.input_tokens)} | {_stats(row.total_tokens)} "
            f"| {_stats(row.active_seconds)} |"
        )
    if baseline is not None:
        lines += _comparison(baseline, run)
    lines += [
        "",
        "## Every repetition",
        "",
        "| scenario | rep | turn | run | status | queries ok/rejected/failed "
        "| requests "
        "(failed, fallback) | tokens in/out | active s | wall s | restarts "
        "| figures | targets | tools |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
        "| --- | --- | --- |",
    ]
    for rep in run.repetitions:
        if rep.error:
            lines.append(
                f"| {rep.scenario_id} | {rep.repetition} | - | - | ERROR "
                f"{rep.error} | | | | | | | | | |"
            )
        for t in rep.turns:
            lines.append(
                f"| {rep.scenario_id} | {rep.repetition} | {t.turn} | "
                f"`{t.run_id}` | {t.run_status} | {t.queries_succeeded}/"
                f"{t.queries_rejected}/{t.queries_failed} | {t.model_requests} "
                f"({t.model_requests_failed}, {t.fallbacks}) | {t.input_tokens:,}/"
                f"{t.output_tokens:,} | {t.active_seconds} | {t.wall_seconds} | "
                f"{len(t.restarts)} | {_figures(t)} | {_targets(t)} | "
                f"{' '.join(t.tools)} |"
            )
    return "\n".join(lines) + "\n"


def _comparison(baseline: EfficiencyRun, candidate: EfficiencyRun) -> list[str]:
    before, after = aggregate(baseline), aggregate(candidate)
    lines = [
        "",
        f"## Compared with baseline `{baseline.label}` (code "
        f"`{baseline.code_revision}`)",
        "",
        "Medians; matched scenario turns only.",
        "",
        "| scenario | turn | input tokens before -> after | total tokens before "
        "-> after | requests before -> after | queries before -> after "
        "| active s before -> after |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    for key in before:
        if key not in after:
            continue
        b, a = before[key], after[key]
        cells = []
        for metric in (
            "input_tokens",
            "total_tokens",
            "requests",
            "queries",
            "active_seconds",
        ):
            x, y = b.median(metric), a.median(metric)
            change = f" ({(y - x) / x:+.0%})" if x else ""
            cells.append(f"{x:,.0f} -> {y:,.0f}{change}")
        lines.append(f"| {key[0]} | {key[1]} | " + " | ".join(cells) + " |")
    return lines


__all__ = [
    "SCORING_VERSION",
    "ResolvedFigure",
    "RunFacts",
    "SuiteError",
    "TurnStats",
    "aggregate",
    "attempts_for_run",
    "citations",
    "figure_results",
    "released_texts",
    "render_summary",
    "rescore",
    "resolve_figures",
    "restarts_for_run",
    "score_turn",
    "span_value",
    "spend_allows",
    "spend_used",
    "terms_met",
    "turn_targets",
    "unqualified_terms",
    "unscoped_queries",
    "worst_case",
]
