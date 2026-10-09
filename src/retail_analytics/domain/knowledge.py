"""Golden Knowledge: independently versioned, reviewed analytical examples.

An example is a question, the SQL that illustrates the method and a report
that connects evidence to findings. It teaches a method; it is never evidence
about a new question and never grants access to product data.

Lifecycle of one version (each version is reviewed on its own):

    candidate --approve--> published --suspend--> suspended --reinstate--> published
        |                      |                      |
      reject                 retire                 retire        (any state: erase)
        v                      v                      v
    rejected                retired                retired         erased (terminal)

Only a published version may reach the model. Approval, rejection and
reinstatement need a reviewer other than the version's author. A newer
published version supersedes (retires) older ones, so at most one version of
an example is published.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from retail_analytics.domain.access import ProductScope, is_valid_product_id
from retail_analytics.domain.errors import InvalidTransition

EXAMPLE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
OPAQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:\-]{1,200}")
SCHEMA_VERSION_PATTERN = re.compile(r"[A-Za-z0-9._:/\-]{1,100}")
MAX_QUESTION = 2_000
MAX_SQL = 20_000
MAX_METHOD_SUMMARY = 4_000
MAX_RATIONALE = 500


class ReviewStatus(StrEnum):
    CANDIDATE = "candidate"
    PUBLISHED = "published"
    SUSPENDED = "suspended"
    REJECTED = "rejected"
    RETIRED = "retired"
    ERASED = "erased"


class Origin(StrEnum):
    """Honest label of where an example came from."""

    PROJECT_AUTHORED = "project_authored"
    ANALYST = "analyst"
    AGENT_INVESTIGATION = "agent_investigation"


class SourceKind(StrEnum):
    AUTHORED = "authored"
    REPORT = "report"
    INVESTIGATION = "investigation"


class ReviewAction(StrEnum):
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    SUSPEND = "suspend"
    REINSTATE = "reinstate"
    RETIRE = "retire"
    SUPERSEDE = "supersede"
    ERASE = "erase"


class ErasureReason(StrEnum):
    """Stored in place of free text, which could repeat what is being erased."""

    PRIVACY_REQUEST = "privacy_request"
    SENSITIVE_CONTENT = "sensitive_content"
    LEGAL = "legal"


class IndexChangeKind(StrEnum):
    UPSERT = "upsert"
    REMOVE = "remove"


class DeliveryRefusal(StrEnum):
    NOT_FOUND = "not_found"
    NOT_PUBLISHED = "not_published"
    NOT_AUTHORIZED = "not_authorized"
    INCOMPATIBLE = "incompatible"
    STALE_INDEX = "stale_index"
    CONTENT_UNAVAILABLE = "content_unavailable"


def is_valid_example_id(value: str) -> bool:
    return EXAMPLE_ID_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class ExampleRef:
    example_id: str
    version: int

    def __post_init__(self) -> None:
        if not is_valid_example_id(self.example_id) or self.version < 1:
            raise ValueError("invalid example reference")


@dataclass(frozen=True, slots=True)
class KnowledgeAccess:
    """Who may receive an example, enforced separately from the content.

    ``SHARED`` examples are sanitized methods any executive may receive.
    ``RESTRICTED`` examples may only be delivered to an executive whose
    current product scope contains every listed product.
    """

    restricted_product_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not all(is_valid_product_id(p) for p in self.restricted_product_ids):
            raise ValueError("invalid product id in restricted access")

    @property
    def is_shared(self) -> bool:
        return not self.restricted_product_ids

    @classmethod
    def shared(cls) -> KnowledgeAccess:
        return cls()

    @classmethod
    def restricted(cls, product_ids: frozenset[str]) -> KnowledgeAccess:
        if not product_ids:
            raise ValueError("restricted access needs at least one product")
        return cls(product_ids)

    def permits(self, scope: ProductScope) -> bool:
        return self.restricted_product_ids <= scope.product_ids


@dataclass(frozen=True, slots=True)
class MetricRef:
    metric_id: str
    version: int

    def __post_init__(self) -> None:
        if not self.metric_id or self.version < 1:
            raise ValueError("invalid metric reference")


@dataclass(frozen=True, slots=True)
class ApplicabilityContext:
    """What the current question runs against: schema and metric versions."""

    schema_version: str
    metric_versions: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Applicability:
    """Schema and metric versions an example was written for (exact match)."""

    schema_version: str
    metrics: frozenset[MetricRef] = frozenset()

    def __post_init__(self) -> None:
        if SCHEMA_VERSION_PATTERN.fullmatch(self.schema_version) is None:
            raise ValueError("invalid schema version")

    def applies_to(self, context: ApplicabilityContext) -> bool:
        return self.schema_version == context.schema_version and all(
            context.metric_versions.get(m.metric_id) == m.version for m in self.metrics
        )


@dataclass(frozen=True, slots=True)
class Provenance:
    """Opaque references to where an example came from; never content.

    Report deletion does not touch it. ``source_id`` is what lets a privacy
    removal or an incorrect-source finding locate every derived version.
    """

    source_kind: SourceKind
    source_id: str | None = None
    source_version: int | None = None

    def __post_init__(self) -> None:
        if (self.source_kind is SourceKind.AUTHORED) != (self.source_id is None):
            raise ValueError("authored provenance has no source; others need one")
        if self.source_id is not None and not OPAQUE_ID_PATTERN.fullmatch(
            self.source_id
        ):
            raise ValueError("invalid source id")
        if self.source_version is not None and self.source_version < 1:
            raise ValueError("invalid source version")


@dataclass(frozen=True, slots=True)
class ExampleContent:
    """Sanitized trio. Report bytes live in the artifact store."""

    question: str
    sql: str
    method_summary: str
    report_artifact_id: str
    report_artifact_version: int

    def __post_init__(self) -> None:
        for name, value, limit in (
            ("question", self.question, MAX_QUESTION),
            ("sql", self.sql, MAX_SQL),
            ("method_summary", self.method_summary, MAX_METHOD_SUMMARY),
        ):
            if not value.strip() or len(value) > limit:
                raise ValueError(f"{name} must be 1-{limit} characters")
        if self.report_artifact_version < 1 or not self.report_artifact_id:
            raise ValueError("invalid report artifact reference")


def content_digest(
    question: str, sql: str, method_summary: str, report_sha256: str
) -> str:
    """Pins a version's content, e.g. to what an index entry was built from."""
    h = hashlib.sha256()
    for part in (question, sql, method_summary, report_sha256):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class GoldenVersion:
    example_id: str
    version: int
    status: ReviewStatus
    origin: Origin
    author_id: str
    access: KnowledgeAccess
    applicability: Applicability
    provenance: Provenance
    created_at: datetime
    status_changed_at: datetime
    # None exactly when the version was erased.
    content: ExampleContent | None
    content_digest: str | None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not is_valid_example_id(self.example_id) or self.version < 1:
            raise ValueError("invalid example reference")
        erased = self.status is ReviewStatus.ERASED
        if erased != (self.content is None) or erased != (self.content_digest is None):
            raise ValueError("content is present exactly until the version is erased")

    @property
    def ref(self) -> ExampleRef:
        return ExampleRef(self.example_id, self.version)

    def deliverable_to(
        self, scope: ProductScope, context: ApplicabilityContext
    ) -> DeliveryRefusal | None:
        """Why this version must not reach the model for this scope, if so."""
        if self.status is not ReviewStatus.PUBLISHED or self.content is None:
            return DeliveryRefusal.NOT_PUBLISHED
        if not self.access.permits(scope):
            return DeliveryRefusal.NOT_AUTHORIZED
        if not self.applicability.applies_to(context):
            return DeliveryRefusal.INCOMPATIBLE
        return None


@dataclass(frozen=True, slots=True)
class Transition:
    to_status: ReviewStatus
    index: IndexChangeKind | None
    needs_independent_reviewer: bool


_TRANSITIONS: dict[tuple[ReviewAction, ReviewStatus], Transition] = {
    (ReviewAction.APPROVE, ReviewStatus.CANDIDATE): Transition(
        ReviewStatus.PUBLISHED, IndexChangeKind.UPSERT, True
    ),
    (ReviewAction.REJECT, ReviewStatus.CANDIDATE): Transition(
        ReviewStatus.REJECTED, None, True
    ),
    (ReviewAction.SUSPEND, ReviewStatus.PUBLISHED): Transition(
        ReviewStatus.SUSPENDED, IndexChangeKind.REMOVE, False
    ),
    (ReviewAction.REINSTATE, ReviewStatus.SUSPENDED): Transition(
        ReviewStatus.PUBLISHED, IndexChangeKind.UPSERT, True
    ),
    (ReviewAction.RETIRE, ReviewStatus.PUBLISHED): Transition(
        ReviewStatus.RETIRED, IndexChangeKind.REMOVE, False
    ),
    (ReviewAction.RETIRE, ReviewStatus.SUSPENDED): Transition(
        ReviewStatus.RETIRED, None, False
    ),
    (ReviewAction.SUPERSEDE, ReviewStatus.PUBLISHED): Transition(
        ReviewStatus.RETIRED, IndexChangeKind.REMOVE, False
    ),
    (ReviewAction.SUPERSEDE, ReviewStatus.SUSPENDED): Transition(
        ReviewStatus.RETIRED, None, False
    ),
}


class SelfReview(Exception):
    """The author tried to review their own version."""


def decide(
    action: ReviewAction,
    version: GoldenVersion,
    actor_id: str,
    *,
    self_review_allowed: bool = False,
) -> Transition:
    """The allowed transition for ``action`` on ``version`` by ``actor_id``.

    ``self_review_allowed`` lifts only the independent-reviewer rule, for a
    caller that has already decided an explicit self-publication policy
    applies; every other rule still holds.
    """
    if action is ReviewAction.ERASE:
        if version.status is ReviewStatus.ERASED:
            raise InvalidTransition(
                "golden example", version.status.value, ReviewStatus.ERASED.value
            )
        return Transition(ReviewStatus.ERASED, IndexChangeKind.REMOVE, False)
    transition = _TRANSITIONS.get((action, version.status))
    if transition is None:
        raise InvalidTransition("golden example", version.status.value, action.value)
    if (
        transition.needs_independent_reviewer
        and actor_id == version.author_id
        and not self_review_allowed
    ):
        raise SelfReview
    return transition
