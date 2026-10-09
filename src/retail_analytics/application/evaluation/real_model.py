"""Compact real-model evaluation (T37): attribution, figure checks, summary.

Pure functions over what a real-model run left behind:

- :func:`provider_use` reads telemetry spans of a conversation's runs and says
  which provider actually answered. A conversation counts for the primary
  provider only when nothing else served it; fallback runs are labelled.
- :func:`figure_checks` compares each independently computed expected figure
  of a scenario (frozen-extract or fixture reference SQL) with the released
  evidence cells *and* with the released answer/report text. It never looks at
  definition stamps or column names: a stamp says which definition was in
  context, not that the SQL implemented it, and a correct evidence row does not
  prove the answer states it.
- :func:`render_summary` writes the public Markdown table.

Boolean report-element flags stay what they are (text heuristics, see
``agent_observation``); they are reported by the runner checks, not here.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

from retail_analytics.application.contracts.evaluation import (
    ConversationOutcome,
    FigureCheck,
    ObservedTable,
    ProviderLabel,
    ProviderUse,
    RealModelEvaluation,
    RecordedSpan,
)
from retail_analytics.application.contracts.telemetry import Span
from retail_analytics.application.evaluation.manifest import (
    ExactExpectation,
    NumericExpectation,
    Scenario,
    TextExpectation,
)

MODEL_ATTEMPT_SPAN = Span.MODEL_ATTEMPT.value
RUN_SPAN = Span.RUN.value
_NONE = "none"
# A stated figure may be rounded, but not by more than this share of its value.
MAX_ROUNDING_SHARE = 0.01
_NUMBER = re.compile(
    r"(?<![\w.])(-?\d{1,3}(?:,\d{3})+|-?\d+)(\.\d+)?\s*(%|[kK]\b|thousand\b)?"
)


def provider_use(
    spans: Iterable[RecordedSpan], run_ids: Sequence[str], primary: str
) -> ProviderUse:
    runs = set(run_ids)
    attempts: Counter[str] = Counter()
    served: set[str] = set()
    reasons: list[str] = []
    answered: dict[str, str] = {}
    for span in spans:
        if span.run_id not in runs:
            continue
        attrs = span.attributes
        if span.name == MODEL_ATTEMPT_SPAN:
            provider = str(attrs.get("provider", "unknown"))
            outcome = str(attrs.get("outcome", "unknown"))
            attempts[f"{provider}:{attrs.get('model', 'unknown')}:{outcome}"] += 1
            if outcome == "succeeded":
                served.add(provider)
            reason = str(attrs.get("fallback_reason", _NONE))
            if reason != _NONE:
                reasons.append(reason)
        elif span.name == RUN_SPAN and "answered_by" in attrs:
            answered[span.run_id] = str(attrs["answered_by"])
    served |= set(answered.values())
    label: ProviderLabel
    if not served:
        label = "unattributed"
    elif served == {primary}:
        label = "primary"
    elif primary not in served:
        label = "fallback"
    else:
        label = "mixed"
    return ProviderUse(
        label=label,
        answered_by=tuple(answered[r] for r in run_ids if r in answered),
        attempts=dict(sorted(attempts.items())),
        fallback_reasons=tuple(dict.fromkeys(reasons)),
    )


def _close(value: float, expected: float, tol: float) -> bool:
    return math.isfinite(value) and abs(abs(value) - abs(expected)) <= tol


def _is_share(name: str, expected: float) -> bool:
    return abs(expected) < 1 and ("share" in name or "rate" in name)


def number_in_evidence(
    expected: float, tol: float, tables: Iterable[ObservedTable], *, share: bool
) -> bool:
    """Some numeric evidence cell equals the figure (a share also as percent).

    Signs are ignored: a change may be computed in either direction; the text
    check and the human review judge the direction stated.
    """
    for table in tables:
        for row in table.rows:
            for cell in row:
                if isinstance(cell, bool) or not isinstance(cell, int | float):
                    continue
                value = float(cell)
                if _close(value, expected, tol) or (
                    share and _close(value, expected * 100, tol * 100)
                ):
                    return True
    return False


def stated_numbers(text: str) -> list[tuple[float, float, bool]]:
    """(value, half rounding unit, is percent) for each number in ``text``."""
    found: list[tuple[float, float, bool]] = []
    for match in _NUMBER.finditer(text):
        whole, fraction, suffix = match.group(1), match.group(2) or "", match.group(3)
        try:
            value = float(whole.replace(",", "") + fraction)
        except ValueError:
            continue
        decimals = len(fraction) - 1 if fraction else 0
        half_unit = 0.5 * 10.0**-decimals
        if suffix and suffix.casefold() in ("k", "thousand"):
            value, half_unit = value * 1000, half_unit * 1000
        found.append((value, half_unit, suffix == "%"))
    return found


def number_in_text(expected: float, tol: float, text: str, *, share: bool) -> bool:
    """The text states the figure, allowing display rounding up to 1%.

    A share may be written as a percent (0.065 -> 6.5%).
    """
    targets = [(expected, tol)]
    if share:
        targets.append((expected * 100, tol * 100))
    for value, half_unit, _ in stated_numbers(text):
        for target, target_tol in targets:
            limit = max(target_tol, half_unit)
            if (
                abs(abs(value) - abs(target)) <= limit + 1e-9
                and abs(abs(value) - abs(target))
                <= MAX_ROUNDING_SHARE * abs(target) + target_tol + 1e-9
            ):
                return True
    return False


_DASHES = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


def _fold(text: str) -> str:
    """Case- and dash-insensitive form (an en dash in "65\u201369" is a hyphen)."""
    return text.translate(_DASHES).casefold()


def _label_in_evidence(label: str, tables: Iterable[ObservedTable]) -> bool:
    folded = _fold(label)
    return any(
        isinstance(cell, str) and _fold(cell) == folded
        for table in tables
        for row in table.rows
        for cell in row
    )


def figure_checks(
    scenario: Scenario, answer_text: str, tables: Sequence[ObservedTable]
) -> tuple[FigureCheck, ...]:
    """Independent checks of every expected figure and named label.

    Booleans (report-element and safety flags) and ``not_contains`` canaries
    are not figures and are skipped here.
    """
    folded = _fold(answer_text)
    checks: list[FigureCheck] = []
    for exp in scenario.expectations:
        if isinstance(exp, NumericExpectation) or (
            isinstance(exp, ExactExpectation)
            and isinstance(exp.expected, int | float)
            and not isinstance(exp.expected, bool)
        ):
            expected = float(exp.expected)  # type: ignore[arg-type]
            tol = max(getattr(exp, "abs_tol", 0.0), 0.005)
            share = _is_share(exp.name, expected)
            checks.append(
                FigureCheck(
                    name=exp.name,
                    kind="number",
                    expected=expected,
                    in_evidence=number_in_evidence(expected, tol, tables, share=share),
                    in_answer=number_in_text(expected, tol, answer_text, share=share),
                )
            )
        elif isinstance(exp, ExactExpectation) and isinstance(exp.expected, str):
            checks.append(
                FigureCheck(
                    name=exp.name,
                    kind="label",
                    in_evidence=_label_in_evidence(exp.expected, tables),
                    in_answer=_fold(exp.expected) in folded,
                )
            )
        elif isinstance(exp, TextExpectation) and exp.kind == "contains":
            checks.append(
                FigureCheck(
                    name=exp.name,
                    kind="label",
                    in_answer=_fold(exp.needle) in folded,
                )
            )
    return tuple(checks)


# -- summary ----------------------------------------------------------------


def _count(checks: Iterable[FigureCheck], attr: str) -> tuple[int, int]:
    values = [getattr(c, attr) for c in checks if getattr(c, attr) is not None]
    return sum(bool(v) for v in values), len(values)


def _fraction(pair: tuple[int, int]) -> str:
    return f"{pair[0]}/{pair[1]}" if pair[1] else "n/a"


def _headline(conversations: Sequence[ConversationOutcome], title: str) -> list[str]:
    figures = [f for c in conversations for f in c.figures]
    numbers = [f for f in figures if f.kind == "number"]
    statuses = Counter(c.runner_status for c in conversations)
    strict = (
        sum(c.strict_checks_passed for c in conversations),
        sum(c.strict_checks_total for c in conversations),
    )
    # Any privacy/scope flag (including heuristic ones that may be false alarms).
    leaks = sum(1 for c in conversations if any(c.safety_flags.values()))
    labels = [f for f in figures if f.kind == "label"]
    in_evidence = _fraction(_count(numbers, "in_evidence"))
    stated = _fraction(_count(numbers, "in_answer"))
    return [
        f"**{title}** ({len(conversations)} conversations)",
        "",
        f"- Numbers found in released evidence: {in_evidence}",
        f"- Numbers stated in the answer or report: {stated}",
        "- Labels (names, categories, bands) stated: "
        + _fraction(_count(labels, "in_answer")),
        f"- Strict named-observation checks (runner): {_fraction(strict)}",
        "- Runner statuses: "
        + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())),
        "- Conversations with a privacy or scope flag raised: "
        f"{leaks}/{len(conversations)}",
        "",
    ]


def render_summary(result: RealModelEvaluation) -> str:
    """Markdown summary: headline split by serving provider, then per case."""
    primary = [c for c in result.conversations if c.provider.label == "primary"]
    other = [c for c in result.conversations if c.provider.label != "primary"]
    lines = [
        f"Recorded {result.recorded_on}; code `{result.code_version}`; target "
        f"`{result.target_id}` (execution backend `{result.execution_backend}`); "
        f"primary provider `{result.primary_provider}`.",
        "",
        *_headline(
            primary,
            f"Answered only by the primary provider ({result.primary_provider})",
        ),
    ]
    if other:
        lines += _headline(
            other, "Fallback, mixed or unattributed (not counted as primary)"
        )
    lines += [
        "| scenario | suite | provider | runs | runner | numbers in evidence "
        "| numbers stated | labels stated | requests | latency s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in result.conversations:
        numbers = [f for f in c.figures if f.kind == "number"]
        labels = [f for f in c.figures if f.kind == "label"]
        runner = c.runner_status + (f" ({c.runner_reason})" if c.runner_reason else "")
        lines.append(
            f"| {c.scenario_id} | {c.suite} | {c.provider.label} "
            f"| {', '.join(c.run_statuses) or '-'} | {runner} "
            f"| {_fraction(_count(numbers, 'in_evidence'))} "
            f"| {_fraction(_count(numbers, 'in_answer'))} "
            f"| {_fraction(_count(labels, 'in_answer'))} "
            f"| {c.measurements.get('model_requests', 0):.0f} "
            f"| {c.measurements.get('latency_seconds', 0):.0f} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "MODEL_ATTEMPT_SPAN",
    "RUN_SPAN",
    "figure_checks",
    "number_in_evidence",
    "number_in_text",
    "provider_use",
    "render_summary",
    "stated_numbers",
]
