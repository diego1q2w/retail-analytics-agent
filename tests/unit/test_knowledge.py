"""Golden Knowledge domain rules and the shared scenarios on an in-memory store."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.application.artifacts import (
    ArtifactMaintenance,
    ArtifactPolicy,
    ArtifactService,
)
from retail_analytics.application.authorization import AccessResolver, OwnershipGuard
from retail_analytics.application.knowledge import (
    GoldenKnowledgeReader,
    KnowledgeService,
)
from retail_analytics.domain.access import ExecutiveAccess, ProductScope, Role
from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.knowledge import (
    Applicability,
    ApplicabilityContext,
    ExampleContent,
    GoldenVersion,
    KnowledgeAccess,
    MetricRef,
    Origin,
    Provenance,
    ReviewAction,
    ReviewStatus,
    SelfReview,
    SourceKind,
    decide,
)
from retail_analytics.domain.sensitive_content import Finding, screen_text
from tests.knowledge_scenarios import SCENARIOS, Harness
from tests.unit.memory_knowledge import MemoryKnowledgeRepository
from tests.unit.test_artifacts import MemoryCatalog

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _version(status: ReviewStatus, author: str = "a") -> GoldenVersion:
    erased = status is ReviewStatus.ERASED
    return GoldenVersion(
        example_id="a" * 32,
        version=1,
        status=status,
        origin=Origin.ANALYST,
        author_id=author,
        access=KnowledgeAccess.shared(),
        applicability=Applicability("s/1"),
        provenance=Provenance(SourceKind.AUTHORED),
        created_at=NOW,
        status_changed_at=NOW,
        content=None if erased else ExampleContent("q", "SELECT 1", "m", "b" * 32, 1),
        content_digest=None if erased else "d" * 64,
    )


def test_lifecycle_transitions_and_reviewer_separation() -> None:
    cand = _version(ReviewStatus.CANDIDATE)
    assert decide(ReviewAction.APPROVE, cand, "r").to_status is ReviewStatus.PUBLISHED
    with pytest.raises(SelfReview):
        decide(ReviewAction.APPROVE, cand, "a")
    with pytest.raises(InvalidTransition):
        decide(ReviewAction.SUSPEND, cand, "r")
    for status in (ReviewStatus.REJECTED, ReviewStatus.RETIRED):
        for action in ReviewAction:
            if action is ReviewAction.ERASE or action is ReviewAction.SUBMIT:
                continue
            with pytest.raises(InvalidTransition):
                decide(action, _version(status), "r")
    for status in ReviewStatus:
        if status is ReviewStatus.ERASED:
            with pytest.raises(InvalidTransition):
                decide(ReviewAction.ERASE, _version(status), "r")
        else:
            assert decide(ReviewAction.ERASE, _version(status), "r")


def test_erased_versions_carry_no_content_and_others_must() -> None:
    assert _version(ReviewStatus.ERASED).content is None
    with pytest.raises(ValueError, match="erased"):
        replace(_version(ReviewStatus.ERASED), content_digest="d")


def test_access_and_applicability_are_exact() -> None:
    restricted = KnowledgeAccess.restricted(frozenset({"1", "2"}))
    assert restricted.permits(ProductScope(frozenset({"1", "2", "3"}), 1))
    assert not restricted.permits(ProductScope(frozenset({"1"}), 1))
    assert not restricted.permits(ProductScope(frozenset(), 1))
    assert KnowledgeAccess.shared().permits(ProductScope(frozenset(), 0))
    with pytest.raises(ValueError):
        KnowledgeAccess.restricted(frozenset())
    with pytest.raises(ValueError):
        KnowledgeAccess.restricted(frozenset({"abc"}))
    app = Applicability("s/1", frozenset({MetricRef("revenue", 2)}))
    assert app.applies_to(ApplicabilityContext("s/1", {"revenue": 2}))
    assert not app.applies_to(ApplicabilityContext("s/1", {"revenue": 1}))
    assert not app.applies_to(ApplicabilityContext("s/2", {"revenue": 2}))


def test_provenance_holds_only_opaque_references() -> None:
    with pytest.raises(ValueError):
        Provenance(SourceKind.REPORT)
    with pytest.raises(ValueError):
        Provenance(SourceKind.AUTHORED, "x")
    with pytest.raises(ValueError):
        Provenance(SourceKind.REPORT, "has spaces and Jane Doe")


@pytest.mark.parametrize(
    ("text", "finding"),
    [
        ("mail jane.doe@example.com now", Finding.EMAIL),
        ("call +34 600 123 456", Finding.PHONE),
        ("call (555) 123-4567", Finding.PHONE),
        ("12 Main Street", Finding.ADDRESS),
        ("WHERE id = 482913", Finding.LONG_NUMBER),
        ("WHERE id IN (1, 2, 3)", Finding.ID_LIST),
        ("WHERE customer_id = 7", Finding.IDENTITY_FILTER),
        ("WHERE email LIKE '%@x'", Finding.IDENTITY_FILTER),
    ],
)
def test_screen_flags_sensitive_content(text: str, finding: Finding) -> None:
    assert finding in screen_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "SELECT month, SUM(sale_price) FROM orders WHERE status = 'Complete'",
        "created_at >= '2026-01-01 00:00:00' AND 1.5 > 0.25",
        "Compare Q3 against Q2; revenue rose 12%.",
        "Revenue is 1,234,567 dollars",
    ],
)
def test_screen_allows_ordinary_method_text(text: str) -> None:
    assert not screen_text(text)


class _Directory:
    def __init__(self, access: dict[str, ExecutiveAccess]) -> None:
        self._access = access

    async def find_by_subject(self, issuer: str, subject: str) -> None:
        return None

    async def get(self, executive_id: str) -> ExecutiveAccess | None:
        return self._access.get(executive_id)


class _NoRecords:
    async def get_session(self, session_id: str) -> None:
        return None

    async def get_run(self, run_id: str) -> None:
        return None

    async def get(self, operation_id: str) -> None:
        return None


def _executive(name: str, roles: set[Role], products: set[str]) -> ExecutiveAccess:
    return ExecutiveAccess(
        executive_id=name,
        roles=frozenset(roles),
        product_ids=frozenset(products),
        active=True,
        authorization_version=1,
    )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    people = {
        "author": _executive("exec-author", {Role.EXECUTIVE}, {"1", "2"}),
        "outsider": _executive("exec-outsider", {Role.EXECUTIVE}, {"9"}),
        "reviewer": _executive("exec-reviewer", {Role.REVIEWER}, {"1", "2", "3"}),
        "blind_reviewer": _executive("exec-blind", {Role.REVIEWER}, set()),
        "author_reviewer": _executive(
            "exec-both", {Role.EXECUTIVE, Role.REVIEWER}, {"1"}
        ),
    }
    resolver = AccessResolver(
        _Directory({p.executive_id: p for p in people.values()}),
        OwnershipGuard(_NoRecords(), _NoRecords(), _NoRecords()),
    )
    blobs = LocalBlobStore(tmp_path / "artifacts")
    catalog = MemoryCatalog()
    artifacts = ArtifactService(catalog, blobs, ArtifactPolicy())
    maintenance = ArtifactMaintenance(catalog, blobs)
    repo = MemoryKnowledgeRepository()
    return Harness(
        service=KnowledgeService(
            resolver, repo, artifacts, maintenance, clock=lambda: NOW
        ),
        reader=GoldenKnowledgeReader(repo, artifacts),
        index_source=repo,
        repository=repo,
        artifacts=artifacts,
        maintenance=maintenance,
        ids={k: v.executive_id for k, v in people.items()},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__)
async def test_scenarios_in_memory(
    scenario: Callable[..., Awaitable[None]],
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    params = inspect.signature(scenario).parameters
    args = (harness, monkeypatch) if "monkeypatch" in params else (harness,)
    await scenario(*args)
