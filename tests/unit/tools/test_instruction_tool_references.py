"""Every tool the model is told to use exists in its permission-filtered catalog.

The model-facing text is the investigation policy, each tool's description and
parameter documentation, and every note context assembly can render. A name
mentioned there that the model cannot call (the original compacted-evidence
note pointed at a tool that was never registered) would waste a step or invite
guesswork, so this fails on any such reference.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

from retail_analytics.application.context import (
    ContextOmissions,
    EvidenceDigest,
    ModelContext,
)
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.investigation_runtime import INVESTIGATION_POLICY
from retail_analytics.application.tools import CapabilityRegistry
from retail_analytics.bootstrap.access import AccessServices
from retail_analytics.bootstrap.budgets import build_run_budgets
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.investigations import build_capability_registry
from retail_analytics.bootstrap.persistence import build_persistence
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.disclosure import DisclosureKind
from retail_analytics.domain.request_scope import (
    Admission,
    AdmissionDecision,
    RequestTopic,
)

# A snake_case word that starts with a tool-like verb.
_TOOL_LIKE = re.compile(
    r"\b(?:fetch|read|list|find|search|save|export|propose|confirm|decline|"
    r"inspect|remember|forget|convert|execute|describe|retrieve|lookup|load)"
    r"_[a-z]+(?:_[a-z]+)*\b"
)


def _registry() -> CapabilityRegistry:
    settings = BackendSettings()
    persistence = build_persistence("postgresql+psycopg://u:p@127.0.0.1:1/none")
    access = AccessServices(MagicMock(), MagicMock(), MagicMock())
    evidence = build_evidence(persistence, settings=settings)
    preferences = build_preferences(persistence, access)
    context = build_context(persistence, access, evidence, preferences)
    return build_capability_registry(
        settings,
        persistence,
        access,
        principals=MagicMock(),
        inputs=MagicMock(),
        budgets=build_run_budgets(settings, persistence.budgets),
        evidence=evidence,
        preferences=preferences,
        context=context,
        discovery=MagicMock(),
        queries=MagicMock(),
        artifacts=MagicMock(),
        retriever=MagicMock(),
    )


def _execution(permissions: frozenset[str]) -> ExecutionContext:
    return ExecutionContext(
        executive_id="exec-1",
        permissions=permissions,
        product_scope=ProductScope(frozenset({"1"}), 1),
        correlation=Correlation(session_id="s", run_id="r"),
    )


def _worst_case_context() -> str:
    """Context text with every note and the compacted-evidence line present."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    digest = EvidenceDigest(
        evidence_id="evd_1",
        version=1,
        computed_at=now,
        period=None,
        definitions=(),
        columns=("a",),
        rows=(("1",),),
        total_rows=5,
        truncated_at_source=True,
        compacted=False,
    )
    compacted = replace(digest, rows=(), compacted=True)
    return ModelContext(
        session_id="s",
        run_id="r",
        authorization_version=1,
        request="q",
        admission=Admission(RequestTopic.ANALYSIS, AdmissionDecision.PROCEED),
        preferences=(),
        evidence=(digest, compacted),
        history=(),
        omissions=ContextOmissions(
            history_access_changed=1,
            history_superseded=1,
            history_before_reset=1,
            history_over_budget=1,
            evidence_withheld=1,
            evidence_before_reset=1,
            evidence_over_budget=1,
            request_truncated=True,
            masked=tuple(DisclosureKind),
        ),
        estimated_tokens=1,
    ).render()


def _mentions(text: str) -> set[str]:
    return set(_TOOL_LIKE.findall(text))


def _descriptor_text(registry: CapabilityRegistry, ctx: ExecutionContext) -> str:
    return "\n".join(
        d.description + "\n" + json.dumps(d.parameters) for d in registry.catalog(ctx)
    )


def test_every_tool_named_in_instructions_and_context_is_callable() -> None:
    registry = _registry()
    full = _execution(frozenset(p.value for p in Permission))
    catalog = {d.name for d in registry.catalog(full)}
    assert "fetch_evidence" in catalog

    text = "\n".join(
        [INVESTIGATION_POLICY, _worst_case_context(), _descriptor_text(registry, full)]
    )
    mentioned = _mentions(text)
    # The scan must be live: the compacted-evidence note names the tool.
    assert "fetch_evidence" in _mentions(_worst_case_context())
    assert {"execute_analysis", "save_report"} <= mentioned
    assert mentioned <= catalog, sorted(mentioned - catalog)


def test_context_notes_only_name_tools_an_analysis_only_user_can_call() -> None:
    registry = _registry()
    analysis_only = _execution(frozenset({Permission.ANALYSIS_READ.value}))
    catalog = {d.name for d in registry.catalog(analysis_only)}
    mentioned = _mentions(_worst_case_context())
    assert mentioned and mentioned <= catalog, sorted(mentioned - catalog)
