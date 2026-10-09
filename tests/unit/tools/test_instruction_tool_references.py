"""Every tool the model is told to use exists in its permission-filtered catalog.

The model-facing text is the investigation policy, each tool's description and
parameter documentation, and every note context assembly can render. A name
mentioned there that the model cannot call (the original compacted-evidence
note pointed at a tool that was never registered) would waste a step or invite
guesswork, so this fails on any such reference.
"""

from __future__ import annotations

import asyncio
import itertools
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
from retail_analytics.application.investigation_policy import (
    catalog_fingerprint,
    render_investigation_policy,
)
from retail_analytics.application.tools import (
    CapabilityRegistry,
    OperationContext,
    ToolCall,
    ToolFailed,
)
from retail_analytics.application.tools.gateway import invoke
from retail_analytics.bootstrap.access import AccessServices
from retail_analytics.bootstrap.budgets import build_run_budgets
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.investigations import build_capability_registry
from retail_analytics.bootstrap.persistence import build_persistence
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.domain.access import (
    Permission,
    ProductScope,
    Role,
    permissions_for,
)
from retail_analytics.domain.disclosure import DisclosureKind
from retail_analytics.domain.operations import ToolErrorCode
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


def _worst_case_context(can_fetch_evidence: bool = True) -> str:
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
    ).render(can_fetch_evidence=can_fetch_evidence)


def _mentions(text: str) -> set[str]:
    return set(_TOOL_LIKE.findall(text))


def _descriptor_text(registry: CapabilityRegistry, ctx: ExecutionContext) -> str:
    return "\n".join(
        d.description + "\n" + json.dumps(d.parameters) for d in registry.catalog(ctx)
    )


def _role_combinations() -> list[tuple[str, frozenset[str]]]:
    """Every combination of server roles, plus a token-narrowed analysis-only set."""
    roles = list(Role)
    combos: list[tuple[str, frozenset[str]]] = []
    for size in range(1, len(roles) + 1):
        for chosen in itertools.combinations(roles, size):
            granted = permissions_for(frozenset(chosen))
            name = "+".join(r.value for r in chosen)
            combos.append((name, frozenset(p.value for p in granted)))
    combos.append(("analysis-only", frozenset({Permission.ANALYSIS_READ.value})))
    return combos


def _principal_text(
    registry: CapabilityRegistry, permissions: frozenset[str]
) -> tuple[set[str], str, str]:
    ctx = _execution(permissions)
    catalog = {d.name for d in registry.catalog(ctx)}
    policy = render_investigation_policy(catalog)
    notes = _worst_case_context(can_fetch_evidence="fetch_evidence" in catalog)
    return catalog, policy, "\n".join([policy, notes, _descriptor_text(registry, ctx)])


def test_every_tool_named_in_instructions_and_context_is_callable() -> None:
    registry = _registry()
    full = frozenset(p.value for p in Permission)
    catalog, _, text = _principal_text(registry, full)
    assert "fetch_evidence" in catalog
    # The scan must be live: the compacted-evidence note names the tool.
    assert "fetch_evidence" in _mentions(_worst_case_context())
    assert {"execute_analysis", "save_report"} <= _mentions(text)
    assert _mentions(text) <= catalog, sorted(_mentions(text) - catalog)


def test_instructions_only_name_tools_in_each_principals_catalog() -> None:
    registry = _registry()
    combos = _role_combinations()
    assert {"executive", "editor", "reviewer", "admin", "analysis-only"} <= {
        name for name, _ in combos
    }
    for name, permissions in combos:
        catalog, policy, text = _principal_text(registry, permissions)
        mentioned = _mentions(text)
        assert mentioned <= catalog, (name, sorted(mentioned - catalog))
        # Guidance exists for tools that are present.
        assert catalog <= _mentions(policy) | _NO_POLICY_GUIDANCE, name


# Present tools whose use the policy does not need to spell out.
_NO_POLICY_GUIDANCE = frozenset(
    {
        "inspect_preferences",
        "forget_preference",
        "decline_preference",
        "read_report",
        "export_report",
    }
)


def test_missing_capabilities_are_described_as_unavailable() -> None:
    registry = _registry()
    _, policy, _ = _principal_text(
        registry, frozenset({Permission.ANALYSIS_READ.value})
    )
    assert "You cannot delete reports for this user" in policy
    assert "propose_report_deletion" not in policy
    # Saving needs analysis:read AND reports:read_own (T18-F4).
    assert "You cannot save reports for this user." in policy
    assert "save_report" not in policy

    _, none_policy, _ = _principal_text(registry, frozenset())
    assert "No analysis tools are available" in none_policy
    assert "execute_analysis" not in none_policy
    assert "You cannot save reports for this user." in none_policy
    assert "You cannot save preferences for this user" in none_policy

    _, full_policy, _ = _principal_text(
        registry, frozenset(p.value for p in Permission)
    )
    assert "You cannot save reports" not in full_policy
    assert "save_report" in full_policy


def test_policy_is_deterministic_per_catalog_fingerprint() -> None:
    registry = _registry()
    seen: dict[str, str] = {}
    for name, permissions in _role_combinations():
        catalog = {d.name for d in registry.catalog(_execution(permissions))}
        first = render_investigation_policy(sorted(catalog))
        second = render_investigation_policy(sorted(catalog, reverse=True))
        assert first == second, name
        fingerprint = catalog_fingerprint(catalog)
        assert fingerprint == catalog_fingerprint(sorted(catalog) * 2)
        assert seen.setdefault(fingerprint, first) == first, name
    # Different catalogs yield different instructions.
    assert len(set(seen.values())) == len(seen)
    assert len(seen) >= 3


def test_saving_needs_both_analysis_and_own_report_read() -> None:
    registry = _registry()
    analysis = Permission.ANALYSIS_READ.value
    own = Permission.REPORTS_READ_OWN.value
    both, _, _ = _principal_text(registry, frozenset({analysis, own}))
    assert "save_report" in both
    for name, permissions in (
        ("analysis-only", frozenset({analysis})),
        ("reports-only", frozenset({own})),
    ):
        catalog, policy, text = _principal_text(registry, permissions)
        assert "save_report" not in catalog, name
        assert "save_report" not in text, name
        assert "You cannot save reports for this user." in policy, name
    # Analysis-only users still get answers.
    analysis_catalog, _, _ = _principal_text(registry, frozenset({analysis}))
    assert "execute_analysis" in analysis_catalog

    executive = permissions_for(frozenset({Role.EXECUTIVE}))
    executive_catalog, executive_policy, _ = _principal_text(
        registry, frozenset(p.value for p in executive)
    )
    assert "save_report" in executive_catalog
    assert "You cannot save reports" not in executive_policy


def test_analysis_only_tool_call_to_save_report_is_refused() -> None:
    from tests.unit.tools.fakes import RecordingSink

    registry = _registry()
    ctx = _execution(frozenset({Permission.ANALYSIS_READ.value}))
    result = asyncio.run(
        invoke(
            registry,
            ToolCall(call_id="c1", name="save_report", arguments={}),
            OperationContext(ctx, "op-1"),
            RecordingSink(),
        )
    )
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is not ToolErrorCode.INVALID_INPUT
