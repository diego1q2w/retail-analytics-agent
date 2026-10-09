"""Output privacy gate: the last check before generated text leaves the backend.

Every generated section (answer text, progress summaries, report content,
anything promoted to memory) passes ``OutputPrivacyGate`` before it is shown,
streamed, saved or promoted. The gate judges the text against a
``DisclosurePolicy`` built from the caller's *current* authority
(``policy_for_run`` re-resolves it; build a new policy for every attempt) and
fails closed:

- citations must name evidence the caller may use now;
- opaque references must occur in evidence the caller may use now (no
  fabricated, stale or other executives' references), and never enter
  cross-executive memory or telemetry;
- figures that only exist in evidence now withheld from the caller
  (narrowed scope, revoked access) are blocked;
- trusted parameter names or key material are blocked;
- direct personal data (names, contact details, identifying addresses or
  locations, raw keys, exact ages, birth dates, encoded identifiers) is
  masked in displayed text and blocks report or memory content, which must be
  regenerated instead of being saved with gaps;
- any failure inside the check withholds the section.

Following the accepted policy, demographics (country, state, age bands) and
small groups are not blocked; there is no minimum group size.

``redact_for_telemetry`` and ``screen_for_memory`` apply the same detectors
to traces and to memory promotion.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.context import user_supplied_terms
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.telemetry import Label, Metric
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.ports.context import MessageHistory
from retail_analytics.application.telemetry import telemetry
from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.disclosure import (
    MASK,
    Detection,
    DisclosureKind,
    ProtectedTerm,
    figures,
    mask,
    normalize,
    numeric_value,
    references,
    scan,
)
from retail_analytics.domain.operations import ToolErrorCode

OUTPUT_POLICY_VERSION = 1
_USER_HISTORY_SCAN = 60


class OutputDestination(StrEnum):
    # Answer sections shown or streamed to the user.
    DISPLAY = "display"
    # Model-supplied progress summaries.
    PROGRESS = "progress"
    # Saved report content (persisted and exportable).
    REPORT = "report"
    # Anything promoted beyond the session (preferences, knowledge, persona).
    MEMORY = "memory"

    @property
    def masks_personal_data(self) -> bool:
        """Displayed text may be masked; persisted content must be regenerated."""
        return self in (OutputDestination.DISPLAY, OutputDestination.PROGRESS)


@dataclass(frozen=True, slots=True)
class OutputSection:
    """One generated section and the evidence it claims to rest on."""

    name: str
    text: str
    cited_evidence: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"OutputSection({self.name!r}, <{len(self.text)} chars>)"


@dataclass(frozen=True, slots=True)
class ReleasedSection:
    name: str
    text: str
    cited_evidence: tuple[str, ...]
    masked: tuple[DisclosureKind, ...]
    masked_spans: int
    policy_version: int = OUTPUT_POLICY_VERSION

    def __repr__(self) -> str:
        return (
            f"ReleasedSection({self.name!r}, <{len(self.text)} chars>, "
            f"masked={[k.value for k in self.masked]})"
        )


class OutputWithheld(Exception):
    """A section cannot be released. ``reason`` is internal; ``message`` safe.

    ``correctable`` means regenerating the section can succeed (for example
    without the personal data or the withdrawn figure).
    """

    _MESSAGES: ClassVar[dict[str, str]] = {
        "unavailable_evidence": "It cites findings that are no longer available",
        "unknown_reference": "It refers to records outside your current access",
        "out_of_scope_figure": "It contains figures outside your current access",
        "internal_secret": "It could not be released safely",
        "personal_data": "It contains personal data that cannot be saved",
        "check_failed": "It could not be checked and was withheld",
    }

    def __init__(
        self, reason: str, *, section: str, code: ToolErrorCode, correctable: bool
    ) -> None:
        self.reason = reason
        self.section = section
        self.code = code
        self.correctable = correctable
        self.message = "This section was withheld. " + self._MESSAGES.get(
            reason, self._MESSAGES["check_failed"]
        )
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"OutputWithheld({self.reason!r}, section={self.section!r})"


@dataclass(frozen=True, slots=True)
class DisclosurePolicy:
    """What may leave now, for one executive, run and authorization version."""

    executive_id: str
    session_id: str
    run_id: str
    authorization_version: int
    usable_evidence: frozenset[str]
    withdrawn_evidence: frozenset[str]
    permitted_references: frozenset[str]
    allowed_figures: frozenset[Decimal]
    withheld_figures: frozenset[Decimal]
    protected_terms: tuple[ProtectedTerm, ...] = ()

    @classmethod
    def from_standings(
        cls,
        *,
        executive_id: str,
        session_id: str,
        run_id: str,
        authorization_version: int,
        standings: Iterable[EvidenceStanding],
        protected_terms: Iterable[ProtectedTerm] = (),
    ) -> DisclosurePolicy:
        usable: set[str] = set()
        withdrawn: set[str] = set()
        refs: set[str] = set()
        allowed: set[Decimal] = set()
        withheld: set[Decimal] = set()
        for standing in standings:
            evidence = standing.evidence
            table = evidence.content.table
            if standing.usable:
                usable.add(evidence.evidence_id)
            else:
                withdrawn.add(evidence.evidence_id)
            for row in table.rows:
                for cell, column in zip(row, table.columns, strict=True):
                    if standing.usable and column.role == "reference":
                        if isinstance(cell, str):
                            refs.add(cell)
                        continue
                    value = numeric_value(cell)
                    if value is None:
                        continue
                    if standing.usable:
                        allowed.add(value)
                    elif standing.access_withdrawn:
                        withheld.add(value)
        return cls(
            executive_id=executive_id,
            session_id=session_id,
            run_id=run_id,
            authorization_version=authorization_version,
            usable_evidence=frozenset(usable),
            withdrawn_evidence=frozenset(withdrawn),
            permitted_references=frozenset(refs),
            allowed_figures=frozenset(allowed),
            withheld_figures=frozenset(withheld),
            protected_terms=tuple(protected_terms),
        )

    def __repr__(self) -> str:
        return (
            f"DisclosurePolicy(run={self.run_id!r}, "
            f"version={self.authorization_version}, "
            f"usable={len(self.usable_evidence)}, "
            f"withdrawn={len(self.withdrawn_evidence)})"
        )


class OutputPrivacyGate:
    def __init__(
        self,
        resolver: AccessResolver,
        evidence: EvidenceService,
        history: MessageHistory,
        *,
        protected_terms: Callable[[], Iterable[ProtectedTerm]] = tuple,
        evidence_scan: int = 200,
    ) -> None:
        self._resolver = resolver
        self._evidence = evidence
        self._history = history
        self._protected_terms = protected_terms
        self._evidence_scan = evidence_scan

    async def policy_for_run(
        self, principal: Principal, run_id: str, *, trace_id: str | None = None
    ) -> DisclosurePolicy:
        """Current disclosure policy for ``run_id``; build one per attempt.

        Raises ``AccessDenied`` when the run is not the caller's or analysis is
        no longer permitted, so nothing can be released for it.
        """
        ctx = await self._resolver.context_for_run(principal, run_id, trace_id=trace_id)
        session_id = ctx.correlation.session_id
        messages = await self._history.recent_messages(session_id, _USER_HISTORY_SCAN)
        session = await self._evidence.session_standing(
            ctx,
            run_ids=[run_id, *(m.run_id for m in messages if m.run_id)],
            limit=self._evidence_scan,
        )
        terms = tuple(self._protected_terms()) + user_supplied_terms(
            m.content for m in messages if m.role is MessageRole.USER
        )
        return DisclosurePolicy.from_standings(
            executive_id=ctx.executive_id,
            session_id=session_id,
            run_id=run_id,
            authorization_version=ctx.product_scope.entitlement_version,
            standings=session.standings,
            protected_terms=terms,
        )

    def release(
        self,
        policy: DisclosurePolicy,
        section: OutputSection,
        destination: OutputDestination,
    ) -> ReleasedSection:
        """The section as it may leave, or ``OutputWithheld``."""
        try:
            return _release(policy, section, destination)
        except OutputWithheld as withheld:
            _count_withheld(withheld.reason, destination)
            raise
        except Exception:
            _count_withheld("check_failed", destination)
            raise OutputWithheld(
                "check_failed",
                section=section.name,
                code=ToolErrorCode.INTERNAL_ERROR,
                correctable=False,
            ) from None

    async def check(
        self,
        principal: Principal,
        run_id: str,
        sections: Sequence[OutputSection],
        destination: OutputDestination,
        *,
        trace_id: str | None = None,
    ) -> tuple[ReleasedSection, ...]:
        """Fresh policy, then all sections or nothing (first withheld raises)."""
        policy = await self.policy_for_run(principal, run_id, trace_id=trace_id)
        return tuple(self.release(policy, s, destination) for s in sections)


def _count_withheld(reason: str, destination: OutputDestination) -> None:
    telemetry().count(
        Metric.GATE_WITHHOLDS,
        {Label.REASON: reason, Label.KIND: destination.value},
    )


def _release(
    policy: DisclosurePolicy, section: OutputSection, destination: OutputDestination
) -> ReleasedSection:
    def withhold(reason: str, code: ToolErrorCode) -> OutputWithheld:
        return OutputWithheld(reason, section=section.name, code=code, correctable=True)

    for evidence_id in section.cited_evidence:
        if evidence_id not in policy.usable_evidence:
            raise withhold("unavailable_evidence", ToolErrorCode.ACCESS_DENIED)
    text = normalize(section.text)
    detections = list(scan(text, policy.protected_terms))
    if any(d.kind is DisclosureKind.INTERNAL_SECRET for d in detections):
        raise OutputWithheld(
            "internal_secret",
            section=section.name,
            code=ToolErrorCode.INTERNAL_ERROR,
            correctable=False,
        )
    for mention in references(text):
        if destination is OutputDestination.MEMORY:
            detections.append(
                Detection(mention.start, mention.end, DisclosureKind.OPAQUE_REFERENCE)
            )
        elif mention.reference not in policy.permitted_references:
            raise withhold("unknown_reference", ToolErrorCode.ACCESS_DENIED)
    if policy.withheld_figures:
        for figure in figures(text):
            if figure.is_trivial:
                continue
            if any(figure.matches(v) for v in policy.withheld_figures) and not any(
                figure.matches(v) for v in policy.allowed_figures
            ):
                raise withhold("out_of_scope_figure", ToolErrorCode.ACCESS_DENIED)
    if detections and not destination.masks_personal_data:
        raise withhold("personal_data", ToolErrorCode.INVALID_INPUT)
    kinds = tuple(sorted({d.kind for d in detections}))
    return ReleasedSection(
        name=section.name,
        text=mask(text, detections) if detections else text,
        cited_evidence=section.cited_evidence,
        masked=kinds,
        masked_spans=len(detections),
    )


def redact_for_telemetry(text: str, protected: Iterable[ProtectedTerm] = ()) -> str:
    """Text safe for traces and logs: personal data and references masked.

    Never raises: if the check fails the whole text is replaced.
    """
    try:
        normalized = normalize(text)
        detections = list(scan(normalized, protected))
        detections += [
            Detection(m.start, m.end, DisclosureKind.OPAQUE_REFERENCE)
            for m in references(normalized)
        ]
        return mask(normalized, detections)
    except Exception:
        return MASK


def screen_for_memory(
    text: str, protected: Iterable[ProtectedTerm] = ()
) -> tuple[DisclosureKind, ...]:
    """Kinds of personal data (and references) that forbid storing ``text``
    beyond the session; empty means it may be promoted. Fails closed."""
    try:
        normalized = normalize(text)
        kinds = {d.kind for d in scan(normalized, protected)}
        if references(normalized):
            kinds.add(DisclosureKind.OPAQUE_REFERENCE)
        return tuple(sorted(kinds))
    except Exception:
        return (DisclosureKind.INTERNAL_SECRET,)
