"""The answer shown when an investigation stops before the model wrote one.

Application-authored and built without any model call (the model budget may
be spent), from evidence already linked to the run. It says what stopped,
which verified results bear on the question and what remains unanswered.

Relevance comes from trusted evidence metadata only - the recorded period and
the recorded definition basis (business terms and metric definitions) - set
against the period and measure words the request names. Being linked to the
run is not relevance: context building links every record the model was
shown, including earlier answers' records. When the request names no period
or measure, only records this run produced are relevant. A record that does
not match is not shown; when none matches, the text says so and gives no
figure.

The recorded definition basis says which definitions the query's fields
belong to, not that the query computed that metric, so results are presented
as verified results with their period and basis, never as "the answer".
Numbers are rounded for reading; amounts carry the source currency as far as
it is known.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.currency import SourceCurrency
from retail_analytics.domain.evidence import EvidenceCell, EvidenceColumn
from retail_analytics.domain.metrics import (
    MetricCatalog,
    Operation,
    Unit,
    UnknownMetricError,
)
from retail_analytics.domain.periods import DateWindow

MAX_RELEVANT = 3
MAX_ROWS = 5
_FUZZY_CUTOFF = 0.8

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_YEAR = re.compile(r"\b(19\d\d|20\d\d|2100)\b")
_WORD = re.compile(r"[a-z]+")
# Metric-id tokens that say nothing about which measure was asked for.
_GENERIC_TOKENS = frozenset({"completed", "per", "of", "and"})
_PERCENT_SUFFIXES = ("_pct", "_percent", "_percentage")
_SHARE_WORDS = ("share", "rate", "ratio")


@dataclass(frozen=True, slots=True)
class RequestFocus:
    """The period and words a request names (no interpretation beyond that)."""

    months: frozenset[int]
    years: frozenset[int]
    words: frozenset[str]

    @classmethod
    def of(cls, request: str) -> RequestFocus:
        text = request.casefold()
        words = frozenset(_WORD.findall(text))
        return cls(
            months=frozenset(_MONTHS[w] for w in words if w in _MONTHS),
            years=frozenset(int(y) for y in _YEAR.findall(text)),
            words=words,
        )

    @property
    def names_period(self) -> bool:
        return bool(self.months or self.years)


@dataclass(frozen=True, slots=True)
class PartialSelection:
    """Records shown (relevant, best first) and how many others were not."""

    relevant: tuple[EvidenceStanding, ...]
    others: int


def select_relevant(
    request: str,
    standings: Sequence[EvidenceStanding],
    *,
    run_id: str,
    limit: int = MAX_RELEVANT,
) -> PartialSelection:
    """The usable records that bear on ``request``; see the module docstring."""
    usable = [s for s in standings if s.usable]
    focus = RequestFocus.of(request)
    vocabularies = {s.evidence.evidence_id: _vocabulary(s) for s in usable}
    names_measure = any(
        _mentions(focus.words, vocabulary) for vocabulary in vocabularies.values()
    )
    if focus.names_period or names_measure:
        matching = [
            s
            for s in usable
            # Without a recorded basis the measure cannot be connected.
            if s.evidence.content.analysis.definitions_recorded
            and (not focus.names_period or _period_matches(s, focus))
            and (
                not names_measure
                or _mentions(focus.words, vocabularies[s.evidence.evidence_id])
            )
        ]
    else:
        matching = [s for s in usable if s.evidence.run_id == run_id]
    matching.sort(
        key=lambda s: (s.evidence.run_id == run_id, s.evidence.computed_at),
        reverse=True,
    )
    chosen: list[EvidenceStanding] = []
    subjects: set[str] = set()
    for standing in matching:
        subject = standing.evidence.content.subject_key
        if subject in subjects:
            continue  # an older computation of the same question
        subjects.add(subject)
        chosen.append(standing)
    relevant = tuple(chosen[:limit])
    return PartialSelection(relevant, len(usable) - len(relevant))


def _vocabulary(standing: EvidenceStanding) -> frozenset[str]:
    analysis = standing.evidence.content.analysis
    if not analysis.definitions_recorded:
        return frozenset()
    words: set[str] = set()
    for term in analysis.terms:
        words.update(_WORD.findall(term.term.casefold()))
    for ref in analysis.definitions:
        words.update(ref.metric_id.casefold().split("_"))
    return frozenset(w for w in words if w and w not in _GENERIC_TOKENS)


def _mentions(words: Iterable[str], vocabulary: frozenset[str]) -> bool:
    if not vocabulary:
        return False
    for word in words:
        if word in vocabulary:
            return True
        if len(word) >= 4 and difflib.get_close_matches(
            word, vocabulary, n=1, cutoff=_FUZZY_CUTOFF
        ):
            return True
    return False


def _is_month(window: DateWindow) -> bool:
    start = window.start
    following = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start.day == 1 and window.end == following


def _period_matches(standing: EvidenceStanding, focus: RequestFocus) -> bool:
    period = standing.evidence.content.analysis.period
    if period is None or period.is_empty:
        return False
    if focus.years and not {period.start.year, period.last_day.year} <= focus.years:
        return False
    if focus.months:
        return _is_month(period) and period.start.month in focus.months
    return True


# -- rendering ----------------------------------------------------------------


def render_partial(
    stop: str,
    selection: PartialSelection,
    *,
    metrics: MetricCatalog,
    currency: SourceCurrency,
    rows: bool = True,
) -> tuple[str, tuple[str, ...]]:
    """The user-facing text and the evidence it cites.

    ``rows=False`` leaves the figures out (a smaller text for the output gate
    to try when the detailed one cannot be released).
    """
    others = _others_line(selection.others)
    if not selection.relevant:
        if selection.others:
            lines = [
                stop,
                "No verified result matches the period and measure in your "
                "question, so no figure is given.",
                others,
            ]
        else:
            lines = [stop, "No verified results were produced yet."]
        lines.append(
            "Your question was not answered. Send it again to continue; a new "
            "request has its own budget."
        )
        return "\n\n".join(line for line in lines if line), ()
    blocks = [stop, "Relevant verified results so far (not a complete answer):"]
    money = False
    cited: list[str] = []
    for standing in selection.relevant:
        lines, has_money = _record_lines(standing, metrics, currency, rows=rows)
        money = money or has_money
        blocks.append("\n".join(lines))
        cited.append(standing.evidence.evidence_id)
    if money:
        blocks.append(_currency_line(currency))
    blocks.append(
        "Not answered: no final answer was written, so these results have not "
        "been checked against every part of your question."
    )
    if others:
        blocks.append(others)
    blocks.append("Evidence: " + ", ".join(cited))
    return "\n\n".join(blocks), tuple(cited)


def _others_line(count: int) -> str:
    if count <= 0:
        return ""
    if count == 1:
        return (
            "1 other result from this conversation covers a different period "
            "or measure and is not shown."
        )
    return (
        f"{count} other results from this conversation cover different periods "
        "or measures and are not shown."
    )


def _currency_line(currency: SourceCurrency) -> str:
    if currency.code is None:
        return "Amounts are in the source currency, which is not verified."
    return f"Amounts are in {currency.label()}."


def _record_lines(
    standing: EvidenceStanding,
    metrics: MetricCatalog,
    currency: SourceCurrency,
    *,
    rows: bool,
) -> tuple[list[str], bool]:
    evidence = standing.evidence
    content = evidence.content
    analysis = content.analysis
    table = content.table
    money_columns = _money_columns(standing, metrics)
    header = [
        analysis.period.describe() + f" ({analysis.time_zone})"
        if analysis.period
        else "several periods"
    ]
    if content.grain:
        header.append("by " + ", ".join(_label(g) for g in content.grain))
    basis = sorted(analysis.definitions) if analysis.definitions_recorded else []
    if basis:
        header.append(
            "definition basis "
            + ", ".join(f"{_label(d.metric_id)} v{d.version}" for d in basis)
        )
    lines = ["- " + "; ".join(header) + ":"]
    if table.truncated:
        lines.append("    The result was cut off: rows are missing from it.")
    if standing.source is not None:
        lines.append(f"    Source: {standing.source.describe(evidence)}.")
    if not rows:
        lines.append(f"    {len(table.rows)} rows (figures not shown).")
        return lines, False
    grain = set(content.grain)
    shown = table.rows[:MAX_ROWS]
    used_money = False
    for row in shown:
        dims = [
            _dimension(cell)
            for cell, column in zip(row, table.columns, strict=True)
            if column.name in grain
        ]
        values: list[str] = []
        for cell, column in zip(row, table.columns, strict=True):
            if column.name in grain:
                continue
            is_money = column.name in money_columns
            shown_value = format_value(cell, column, money=is_money, currency=currency)
            values.append(f"{_label(column.name)} {shown_value}")
            used_money = used_money or (is_money and _is_number(cell))
        prefix = ", ".join(dims) + ": " if dims else ""
        lines.append("  - " + prefix + "; ".join(values))
    if len(table.rows) > len(shown):
        lines.append(f"    (+{len(table.rows) - len(shown)} more rows)")
    if not table.rows:
        lines.append("    No rows.")
    return lines, used_money


def _money_columns(
    standing: EvidenceStanding, metrics: MetricCatalog
) -> frozenset[str]:
    """Value columns read from the measure of a recorded currency definition."""
    analysis = standing.evidence.content.analysis
    if not analysis.definitions_recorded:
        return frozenset()
    measures: set[str] = set()
    for ref in analysis.definitions:
        try:
            definition = metrics.get(ref.metric_id, ref.version)
        except UnknownMetricError:
            continue
        if definition.unit is not Unit.CURRENCY_AMOUNT:
            continue
        if definition.operation is Operation.RATIO:
            parts = (definition.numerator_id, definition.denominator_id)
            for part in parts:
                if part is None:
                    continue
                inner = metrics.get(part)
                if inner.unit is Unit.CURRENCY_AMOUNT and inner.measure_field:
                    measures.add(f"{inner.relation.value}.{inner.measure_field}")
        elif definition.measure_field:
            measures.add(f"{definition.relation.value}.{definition.measure_field}")
    return frozenset(
        c.name
        for c in standing.evidence.content.table.columns
        if c.role == "value" and measures.intersection(c.sources)
    )


def _label(name: str) -> str:
    return name.replace("_", " ").strip()


def _dimension(cell: EvidenceCell) -> str:
    if cell is None:
        return "(none)"
    if isinstance(cell, datetime | date):
        return cell.isoformat()
    return str(cell)


def _is_number(cell: EvidenceCell) -> bool:
    return isinstance(cell, int | float | Decimal) and not isinstance(cell, bool)


def format_value(
    cell: EvidenceCell,
    column: EvidenceColumn,
    *,
    money: bool = False,
    currency: SourceCurrency | None = None,
) -> str:
    """One cell for reading: grouped digits, useful precision, units."""
    if cell is None:
        return "-"
    if isinstance(cell, bool):
        return "yes" if cell else "no"
    if isinstance(cell, datetime | date):
        return cell.isoformat()
    if not _is_number(cell):
        return str(cell)
    if column.role == "reference":
        return str(cell)
    value = Decimal(str(cell)) if isinstance(cell, float) else Decimal(cell)
    if not value.is_finite():
        return str(cell)
    name = column.name.casefold()
    if name.endswith(_PERCENT_SUFFIXES) or name.startswith(("pct_", "percent_")):
        return f"{value:,.1f}%"
    if any(word in name.split("_") for word in _SHARE_WORDS) and abs(value) <= 1:
        return f"{value * 100:,.1f}%"
    if money:
        code = currency.code if currency is not None else None
        return f"{value:,.2f}" + (f" {code}" if code else "")
    if value == value.to_integral_value():
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.3g}"
