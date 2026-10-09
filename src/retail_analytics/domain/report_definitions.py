"""Display-time notices when a saved report's definitions are not current.

A saved report is a historical snapshot: its Markdown and figures never
change. When it is shown, the definitions recorded with its cited evidence
(metric versions and what business terms such as "revenue" meant) are compared
with the reader's *current* definitions. A difference produces a plain-language
notice naming both definitions and saying the figures were not recalculated.
Evidence that recorded no definitions gets a neutral notice instead: unknown
is never reported as "unchanged". Notices are computed on every display and
never stored, so reading is never blocked or altered by a definition change.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from retail_analytics.domain.evidence import DefinitionRef, Evidence
from retail_analytics.domain.metric_preferences import (
    DefinitionPreference,
    resolve_term,
)
from retail_analytics.domain.metrics import (
    MetricCatalog,
    MetricDefinition,
    Operation,
    UnknownMetricError,
)

NOT_RECALCULATED = (
    "The figures in this report have not been recalculated; using your current "
    "definition requires recalculating them."
)
UNRECORDED_MESSAGE = (
    "The definitions used by this report were not recorded, so they cannot be "
    "compared with your current definitions. Its figures have not been "
    "recalculated; recalculate them before relying on them under your current "
    "definitions."
)
_DATE_FIELDS = {"ordered_date": "the date the order was placed"}


class DefinitionNoticeKind(StrEnum):
    # A recorded definition differs from the reader's current one.
    DEFINITION_CHANGED = "definition_changed"
    # The report's evidence did not record its definitions.
    DEFINITIONS_NOT_RECORDED = "definitions_not_recorded"


@dataclass(frozen=True, slots=True)
class DefinitionNotice:
    """One display-time notice. ``message`` is complete, plain-language text;
    the other fields let clients render it consistently."""

    kind: DefinitionNoticeKind
    message: str
    # The business term or metric the notice is about (None when unrecorded).
    subject: str | None = None
    # Plain-language definitions: the report's and the reader's current one.
    report_definition: str | None = None
    current_definition: str | None = None
    recalculation_required: bool = True


def describe_metric(metric: MetricDefinition) -> str:
    """Plain language, e.g. "completed item sales (version 1): the sum of sale
    amount over items with status Complete, dated by the date the order was
    placed"."""
    statuses = " or ".join(sorted(metric.population.qualifying_statuses))
    population = f"items with status {statuses}"
    if metric.operation is Operation.SUM:
        what = f"the sum of {_words(metric.measure_field)} over {population}"
    elif metric.operation is Operation.COUNT_ROWS:
        what = f"the number of {population}"
    elif metric.operation is Operation.COUNT_DISTINCT:
        field = _words(metric.distinct_field).removesuffix(" ref")
        what = f"the number of distinct {field}s with {population}"
    else:
        what = (
            f"{_words(metric.numerator_id)} divided by "
            f"{_words(metric.denominator_id)}, over {population}"
        )
    dated = _DATE_FIELDS.get(metric.time_field, _words(metric.time_field))
    return (
        f"{_words(metric.metric_id)} (version {metric.version}): {what}, "
        f"dated by {dated}"
    )


def definition_notices(
    evidence: Iterable[Evidence],
    catalog: MetricCatalog,
    current: tuple[DefinitionPreference, ...],
) -> tuple[DefinitionNotice, ...]:
    """Notices for the definitions behind ``evidence`` versus the reader's
    ``current`` metric preferences (``EffectivePreferences
    .definition_preferences``) and the catalog's current versions.

    No notice means every recorded definition matches; any evidence without
    recorded definitions adds one neutral notice.
    """
    records = list(evidence)
    unrecorded = any(not e.content.analysis.definitions_recorded for e in records)
    recorded = [
        e.content.analysis for e in records if e.content.analysis.definitions_recorded
    ]
    notices: list[DefinitionNotice] = []
    # Definitions already explained, or that are what a term means now (a
    # pinned older version is the reader's current choice, not a change).
    explained: set[DefinitionRef] = set()
    terms = sorted({t for stamp in recorded for t in stamp.terms})
    for meaning in terms:
        try:
            now = resolve_term(catalog, meaning.term, current).definition
        except UnknownMetricError:
            now = None
        if now is not None and now.key == (
            meaning.definition.metric_id,
            meaning.definition.version,
        ):
            explained.add(meaning.definition)
            continue
        explained.add(meaning.definition)
        notices.append(_term_notice(meaning.term, meaning.definition, now, catalog))
    definitions = sorted({d for stamp in recorded for d in stamp.definitions})
    for ref in definitions:
        if ref in explained:
            continue
        try:
            latest = catalog.get(ref.metric_id)
        except UnknownMetricError:
            latest = None
        if latest is not None and latest.version == ref.version:
            continue
        notices.append(_version_notice(ref, latest, catalog))
    if unrecorded:
        notices.append(
            DefinitionNotice(
                DefinitionNoticeKind.DEFINITIONS_NOT_RECORDED, UNRECORDED_MESSAGE
            )
        )
    return tuple(notices)


def _term_notice(
    term: str,
    used: DefinitionRef,
    now: MetricDefinition | None,
    catalog: MetricCatalog,
) -> DefinitionNotice:
    before = _describe_ref(used, catalog)
    after = (
        describe_metric(now)
        if now is not None
        else "not defined (no approved definition is available)"
    )
    message = (
        f'This report uses "{term}" to mean {before}. Your current definition '
        f'of "{term}" is {after}. {NOT_RECALCULATED}'
    )
    return DefinitionNotice(
        DefinitionNoticeKind.DEFINITION_CHANGED, message, term, before, after
    )


def _version_notice(
    used: DefinitionRef, latest: MetricDefinition | None, catalog: MetricCatalog
) -> DefinitionNotice:
    before = _describe_ref(used, catalog)
    after = (
        describe_metric(latest)
        if latest is not None
        else "no longer an approved definition"
    )
    message = (
        f"This report uses {before}. The current definition is {after}. "
        f"{NOT_RECALCULATED}"
    )
    return DefinitionNotice(
        DefinitionNoticeKind.DEFINITION_CHANGED,
        message,
        used.metric_id,
        before,
        after,
    )


def _describe_ref(ref: DefinitionRef, catalog: MetricCatalog) -> str:
    try:
        return describe_metric(catalog.get(ref.metric_id, ref.version))
    except UnknownMetricError:
        return f"{_words(ref.metric_id)} (version {ref.version})"


def _words(name: str | None) -> str:
    return (name or "").replace("_", " ")
