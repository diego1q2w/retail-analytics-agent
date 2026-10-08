"""Backend-independent Golden Knowledge scenarios.

Run by ``tests/unit`` against an in-memory repository and by
``tests/integration`` against PostgreSQL, so both honor one contract.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest

from retail_analytics.application.artifacts import (
    ArtifactError,
    ArtifactMaintenance,
    ArtifactService,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.knowledge import IndexChange
from retail_analytics.application.knowledge import (
    ApprovalChecks,
    ExampleDraft,
    GoldenKnowledgeReader,
    KnowledgeError,
    KnowledgeErrorCode,
    KnowledgeService,
)
from retail_analytics.application.ports.knowledge import (
    KnowledgeIndexSource,
    KnowledgeRepository,
)
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.knowledge import (
    Applicability,
    ApplicabilityContext,
    DeliveryRefusal,
    ErasureReason,
    ExampleRef,
    GoldenVersion,
    IndexChangeKind,
    KnowledgeAccess,
    MetricRef,
    Origin,
    Provenance,
    ReviewAction,
    ReviewStatus,
    SourceKind,
)

CONTEXT = ApplicabilityContext("store/1", {"revenue": 1})
OK = ApprovalChecks(correct=True, sanitized=True, applicable=True)
SCOPE_ALL = ProductScope(frozenset({"1", "2", "3"}), 1)
SCOPE_NONE = ProductScope(frozenset(), 1)


def principal(executive_id: str, *permissions: Permission) -> Principal:
    return Principal(executive_id, frozenset(p.value for p in permissions))


@dataclass
class Harness:
    """Executives: ``author`` (products 1,2), ``outsider`` (product 9),
    ``reviewer`` (products 1-3), ``blind_reviewer`` (no products),
    ``author_reviewer`` (both roles, product 1)."""

    service: KnowledgeService
    reader: GoldenKnowledgeReader
    index_source: KnowledgeIndexSource
    repository: KnowledgeRepository
    artifacts: ArtifactService
    maintenance: ArtifactMaintenance
    ids: dict[str, str]
    index: FakeIndex = field(init=False)

    def __post_init__(self) -> None:
        self.index = FakeIndex(self.index_source)

    def as_(self, who: str) -> Principal:
        read = Permission.ANALYSIS_READ
        review = Permission.KNOWLEDGE_REVIEW
        scopes = {
            "author": (read,),
            "outsider": (read,),
            "reviewer": (review,),
            "blind_reviewer": (review,),
            "author_reviewer": (read, review),
        }[who]
        return principal(self.ids[who], *scopes)


class FakeIndex:
    """In-memory retrieval index that follows the invalidation feed."""

    def __init__(self, source: KnowledgeIndexSource) -> None:
        self._source = source
        self._seq = 0
        self.entries: dict[ExampleRef, str] = {}
        self.seen: list[IndexChange] = []

    async def sync(self) -> None:
        for doc in await self._source.published_documents(None, 1000):
            self.entries.setdefault(doc.ref, doc.content_digest)
        while changes := await self._source.changes_after(self._seq, 100):
            for c in changes:
                self.seen.append(c)
                self._seq = c.sequence
                ref = ExampleRef(c.example_id, c.version)
                if c.kind is IndexChangeKind.REMOVE:
                    self.entries.pop(ref, None)

    def stale_candidates(self) -> list[tuple[ExampleRef, str | None]]:
        return [(r, d) for r, d in self.entries.items()]


def draft(
    *,
    question: str = "How did monthly revenue trend?",
    sql: str = "SELECT month, SUM(sale_price) AS revenue FROM orders GROUP BY month",
    report: str = "# Revenue trend\n\nCompare complete months; flag partial ones.",
    access: KnowledgeAccess | None = None,
    metrics: frozenset[MetricRef] = frozenset({MetricRef("revenue", 1)}),
    schema: str = "store/1",
    source: Provenance | None = None,
    attested: bool = True,
) -> ExampleDraft:
    return ExampleDraft(
        question=question,
        sql=sql,
        method_summary="Aggregate completed item sales by month and compare.",
        report_markdown=report,
        applicability=Applicability(schema, metrics),
        access=access or KnowledgeAccess.shared(),
        origin=Origin.ANALYST,
        provenance=source or Provenance(SourceKind.AUTHORED),
        sanitization_attested=attested,
    )


def key() -> str:
    return "k-" + uuid.uuid4().hex


async def submit(
    h: Harness, d: ExampleDraft | None = None, who: str = "author", **kw: str
) -> GoldenVersion:
    return await h.service.submit_candidate(
        h.as_(who), d or draft(), idempotency_key=key(), **kw
    )


async def publish(h: Harness, version: GoldenVersion) -> GoldenVersion:
    result = await h.service.approve(
        h.as_("reviewer"), version.ref, rationale="Method is sound.", checks=OK
    )
    return result.version


async def delivered(
    h: Harness, version: GoldenVersion, scope: ProductScope = SCOPE_ALL
) -> tuple[int, list[DeliveryRefusal]]:
    d = await h.reader.deliver(scope, CONTEXT, [(version.ref, version.content_digest)])
    return len(d.examples), [r.reason for r in d.refused]


# -- scenarios -----------------------------------------------------------------


async def candidates_are_not_golden_until_an_authorized_reviewer_publishes(
    h: Harness,
) -> None:
    v = await submit(h)
    assert v.status is ReviewStatus.CANDIDATE and v.version == 1
    assert await delivered(h, v) == (0, [DeliveryRefusal.NOT_PUBLISHED])
    await h.index.sync()
    assert v.ref not in h.index.entries

    with pytest.raises(AccessDenied):  # an ordinary executive cannot publish
        await h.service.approve(h.as_("author"), v.ref, rationale="ok", checks=OK)
    with pytest.raises(KnowledgeError) as incomplete:
        await h.service.approve(
            h.as_("reviewer"),
            v.ref,
            rationale="ok",
            checks=ApprovalChecks(True, False, True),
        )
    assert incomplete.value.code is KnowledgeErrorCode.INVALID_REQUEST
    assert await delivered(h, v) == (0, [DeliveryRefusal.NOT_PUBLISHED])

    published = await publish(h, v)
    assert published.status is ReviewStatus.PUBLISHED
    assert published.reviewed_by == h.ids["reviewer"]
    assert await delivered(h, published) == (1, [])
    await h.index.sync()
    assert published.ref in h.index.entries


async def authors_cannot_review_their_own_examples(h: Harness) -> None:
    v = await submit(h, who="author_reviewer")
    for action in (h.service.approve, h.service.reject):
        kwargs = {"checks": OK} if action == h.service.approve else {}
        with pytest.raises(KnowledgeError) as error:
            await action(h.as_("author_reviewer"), v.ref, rationale="mine", **kwargs)
        assert error.value.code is KnowledgeErrorCode.SELF_REVIEW
    rejected = await h.service.reject(
        h.as_("reviewer"), v.ref, rationale="Not general enough."
    )
    assert rejected.version.status is ReviewStatus.REJECTED
    assert await delivered(h, v) == (0, [DeliveryRefusal.NOT_PUBLISHED])
    with pytest.raises(InvalidTransition):
        await h.service.approve(
            h.as_("reviewer"), v.ref, rationale="Changed my mind.", checks=OK
        )
    actions = [e.action for e in await h.service.history(h.as_("reviewer"), v.ref)]
    assert actions == [ReviewAction.SUBMIT, ReviewAction.REJECT]


async def sensitive_content_is_refused_without_echoing_it(h: Harness) -> None:
    bad = {
        "question": draft(question="Spend for jane.doe@example.com last month?"),
        "sql": draft(sql="SELECT * FROM orders WHERE user_id = 482913"),
        "id_list": draft(sql="SELECT 1 FROM p WHERE id IN (11, 22, 33)"),
        "report": draft(report="# R\n\nCall +34 600 123 456 about 12 Main Street."),
    }
    for name, d in bad.items():
        with pytest.raises(KnowledgeError) as error:
            await submit(h, d)
        assert error.value.code is KnowledgeErrorCode.SENSITIVE_CONTENT, name
        text = str(error.value) + repr(error.value.findings)
        for secret in ("jane.doe", "482913", "600 123", "Main Street"):
            assert secret not in text
    with pytest.raises(KnowledgeError) as unattested:
        await submit(h, draft(attested=False))
    assert unattested.value.code is KnowledgeErrorCode.INVALID_REQUEST
    with pytest.raises(KnowledgeError):  # reviewer rationale is screened too
        v = await submit(h)
        await h.service.reject(
            h.as_("reviewer"), v.ref, rationale="ask jane.doe@example.com"
        )


async def restricted_examples_follow_the_recipients_own_product_scope(
    h: Harness,
) -> None:
    restricted = draft(access=KnowledgeAccess.restricted(frozenset({"1", "2"})))
    v = await submit(h, restricted)
    with pytest.raises(AccessDenied):  # authors cannot restrict to products
        await submit(h, draft(access=KnowledgeAccess.restricted(frozenset({"9"}))))
    with pytest.raises(AccessDenied):  # a reviewer must be able to see it
        await h.service.approve(
            h.as_("blind_reviewer"), v.ref, rationale="ok", checks=OK
        )
    v = await publish(h, v)
    assert await delivered(h, v, ProductScope(frozenset({"1", "2"}), 3)) == (1, [])
    for scope in (
        ProductScope(frozenset({"1"}), 3),
        ProductScope(frozenset({"9"}), 3),
        SCOPE_NONE,
    ):
        assert await delivered(h, v, scope) == (0, [DeliveryRefusal.NOT_AUTHORIZED])
    # publication changes nobody's access
    assert SCOPE_NONE.is_empty
    shared = await publish(h, await submit(h))
    assert await delivered(h, shared, SCOPE_NONE) == (1, [])  # method, no data


async def incompatible_schema_or_metric_versions_are_not_delivered(
    h: Harness,
) -> None:
    v = await publish(h, await submit(h))
    for context in (
        ApplicabilityContext("store/2", {"revenue": 1}),
        ApplicabilityContext("store/1", {"revenue": 2}),
        ApplicabilityContext("store/1", {}),
    ):
        d = await h.reader.deliver(SCOPE_ALL, context, [(v.ref, v.content_digest)])
        assert [r.reason for r in d.refused] == [DeliveryRefusal.INCOMPATIBLE]


async def new_versions_are_independent_and_supersede_the_old_one(
    h: Harness,
) -> None:
    v1 = await publish(h, await submit(h))
    v2 = await submit(h, draft(report="# Revised\n\nUse comparable periods."),
                      example_id=v1.example_id)  # fmt: skip
    assert v2.version == 2 and v2.status is ReviewStatus.CANDIDATE
    assert await delivered(h, v1) == (1, [])  # the old version still serves
    with pytest.raises(AccessDenied):  # only the author versions an example
        await submit(h, draft(), who="outsider", example_id=v1.example_id)
    result = await h.service.approve(
        h.as_("reviewer"), v2.ref, rationale="Better.", checks=OK
    )
    assert result.superseded == (v1.ref,)
    assert await delivered(h, v1) == (0, [DeliveryRefusal.NOT_PUBLISHED])
    assert await delivered(h, result.version) == (1, [])
    await h.index.sync()
    assert v1.ref not in h.index.entries and v2.ref in h.index.entries


async def submission_is_idempotent(h: Harness) -> None:
    k = key()
    first = await h.service.submit_candidate(
        h.as_("author"), draft(), idempotency_key=k
    )
    again = await h.service.submit_candidate(
        h.as_("author"), draft(), idempotency_key=k
    )
    assert again == first
    with pytest.raises(KnowledgeError) as error:
        await h.service.submit_candidate(
            h.as_("author"), draft(question="A different question?"), idempotency_key=k
        )
    assert error.value.code is KnowledgeErrorCode.IDEMPOTENCY_CONFLICT


async def source_deletion_preserves_examples_but_incorrect_source_suspends(
    h: Harness,
) -> None:
    report_id = uuid.uuid4().hex
    source = Provenance(SourceKind.REPORT, report_id, 3)
    v = await publish(h, await submit(h, draft(source=source), who="author_reviewer"))
    other = await publish(h, await submit(h))

    # the saved report is deleted by its owner: nothing here changes
    original = await h.artifacts.save(
        h.ids["author"],
        media_type="text/markdown",
        content=b"# mine",
        idempotency_key=key(),
    )
    await h.maintenance.purge(original.artifact_id)
    assert await delivered(h, v) == (1, [])

    # a reviewer finds the source wrong
    with pytest.raises(AccessDenied):
        await h.service.suspend_by_source(
            h.as_("author"), SourceKind.REPORT, report_id, rationale="wrong"
        )
    suspended = await h.service.suspend_by_source(
        h.as_("reviewer"), SourceKind.REPORT, report_id, rationale="Source was wrong."
    )
    assert suspended == (v.ref,)
    assert await delivered(h, v) == (0, [DeliveryRefusal.NOT_PUBLISHED])
    assert await delivered(h, other) == (1, [])
    await h.index.sync()
    assert v.ref not in h.index.entries and other.ref in h.index.entries

    with pytest.raises(KnowledgeError) as self_review:  # needs a second person
        await h.service.reinstate(h.as_("author_reviewer"), v.ref, rationale="fixed")
    assert self_review.value.code is KnowledgeErrorCode.SELF_REVIEW
    back = await h.service.reinstate(
        h.as_("reviewer"), v.ref, rationale="Source corrected."
    )
    assert back.version.status is ReviewStatus.PUBLISHED
    assert await delivered(h, v) == (1, [])
    retired = await h.service.retire(h.as_("reviewer"), v.ref, rationale="Outdated.")
    assert retired.version.status is ReviewStatus.RETIRED
    assert await delivered(h, v) == (0, [DeliveryRefusal.NOT_PUBLISHED])


async def stale_index_candidates_never_reach_the_model(h: Harness) -> None:
    v1 = await publish(h, await submit(h))
    live = await publish(h, await submit(h))
    erased = await publish(h, await submit(h))
    changed = await publish(h, await submit(h))
    await h.index.sync()
    mine = {v1.ref, live.ref, erased.ref, changed.ref}
    # the index snapshot before the changes (the database may hold other examples)
    stale = [(r, d) for r, d in h.index.stale_candidates() if r in mine]
    assert len(stale) == 4
    await h.service.suspend(h.as_("reviewer"), live.ref, rationale="Under review.")
    await h.service.erase(h.as_("reviewer"), erased.ref, reason=ErasureReason.LEGAL)
    pinned = [(r, "0" * 64 if r == changed.ref else d) for r, d in stale]
    d = await h.reader.deliver(SCOPE_ALL, CONTEXT, pinned)
    assert [e.ref for e in d.examples] == [v1.ref]
    reasons = {r.ref: r.reason for r in d.refused}
    assert reasons[live.ref] is DeliveryRefusal.NOT_PUBLISHED
    assert reasons[erased.ref] is DeliveryRefusal.NOT_PUBLISHED
    assert reasons[changed.ref] is DeliveryRefusal.STALE_INDEX


async def privacy_erasure_removes_content_report_and_index_entries(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_q = "Which customers returned the Zebra Parka?"
    report_id = uuid.uuid4().hex
    source = Provenance(SourceKind.INVESTIGATION, report_id, 1)
    v = await publish(h, await submit(h, draft(question=secret_q, source=source)))
    sibling = await submit(h, draft(source=source))
    unrelated = await publish(h, await submit(h))
    await h.index.sync()
    assert v.ref in h.index.entries and v.content is not None
    artifact_id = v.content.report_artifact_id

    real_purge = h.maintenance.purge
    calls = {"n": 0}

    async def flaky(artifact: str) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk went away")
        return await real_purge(artifact)

    monkeypatch.setattr(h.maintenance, "purge", flaky)
    with pytest.raises(OSError):
        await h.service.erase(
            h.as_("reviewer"), v.ref, reason=ErasureReason.PRIVACY_REQUEST
        )
    monkeypatch.undo()
    # content is already unreachable although the purge is unfinished
    gone = await h.repository.get(v.ref)
    assert gone is not None and gone.status is ReviewStatus.ERASED
    assert await delivered(h, v) == (0, [DeliveryRefusal.NOT_PUBLISHED])
    assert await h.repository.pending_purges(10)

    refs = await h.service.erase_by_source(  # the rerun finishes the job
        h.as_("reviewer"), SourceKind.INVESTIGATION, report_id,
        reason=ErasureReason.PRIVACY_REQUEST,
    )  # fmt: skip
    assert set(refs) == {v.ref, sibling.ref}
    assert not await h.repository.pending_purges(10)
    for ref in refs:
        stored = await h.repository.get(ref)
        assert stored is not None
        assert stored.status is ReviewStatus.ERASED
        assert stored.content is None and stored.content_digest is None
        # provenance keeps only opaque references; audit keeps no content
        assert stored.provenance == source
        events = await h.service.history(h.as_("reviewer"), ref)
        assert secret_q not in repr(events)
        assert events[-1].rationale == ErasureReason.PRIVACY_REQUEST.value
    with pytest.raises((ArtifactError, AccessDenied)):
        await h.artifacts.read("system:golden-knowledge", artifact_id)
    await h.index.sync()
    assert v.ref not in h.index.entries and unrelated.ref in h.index.entries
    assert any(
        c.kind is IndexChangeKind.REMOVE and c.reason is ReviewAction.ERASE
        for c in h.index.seen
    )
    with pytest.raises(InvalidTransition):
        await h.service.approve(
            h.as_("reviewer"), sibling.ref, rationale="x", checks=OK
        )
    again = await h.service.erase(h.as_("reviewer"), v.ref, reason=ErasureReason.LEGAL)
    assert again.status is ReviewStatus.ERASED  # repeating is harmless


SCENARIOS: list[Callable[..., Awaitable[None]]] = [
    candidates_are_not_golden_until_an_authorized_reviewer_publishes,
    authors_cannot_review_their_own_examples,
    sensitive_content_is_refused_without_echoing_it,
    restricted_examples_follow_the_recipients_own_product_scope,
    incompatible_schema_or_metric_versions_are_not_delivered,
    new_versions_are_independent_and_supersede_the_old_one,
    submission_is_idempotent,
    source_deletion_preserves_examples_but_incorrect_source_suspends,
    stale_index_candidates_never_reach_the_model,
    privacy_erasure_removes_content_report_and_index_entries,
]
