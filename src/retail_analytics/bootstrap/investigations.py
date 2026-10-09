"""Composition of guarded investigation services and the durable agent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai.models import Model

from retail_analytics.adapters.models.budgeted import BudgetedModel
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigations import (
    PostgresInvestigationInputs,
    PostgresRunPrincipals,
)
from retail_analytics.adapters.temporal.activities import bind_runtime
from retail_analytics.adapters.temporal.agent import AgentServices, bind_agent_services
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.discovery import DiscoveryService
from retail_analytics.application.investigation_runtime import InvestigationRuntime
from retail_analytics.application.investigations import (
    InvestigationControl,
    InvestigationLauncher,
)
from retail_analytics.application.ports.currency_conversion import (
    ExchangeRateProvider,
)
from retail_analytics.application.ports.investigations import InvestigationScheduler
from retail_analytics.application.query_execution import QueryExecutionService
from retail_analytics.application.retrieval import GoldenRetriever
from retail_analytics.application.tool_runner import ToolRunner
from retail_analytics.application.tools import CapabilityRegistry, CapabilitySpec
from retail_analytics.bootstrap.access import AccessServices
from retail_analytics.bootstrap.artifacts import ArtifactServices
from retail_analytics.bootstrap.budgets import build_run_budgets
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.currency import build_currency_conversion
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.bootstrap.report_deletion import build_report_deletion
from retail_analytics.bootstrap.reports import build_reports
from retail_analytics.capabilities.analysis import analysis_capability
from retail_analytics.capabilities.currency import currency_capability
from retail_analytics.capabilities.discovery import discovery_capabilities
from retail_analytics.capabilities.preferences import preference_capabilities
from retail_analytics.capabilities.report_deletion import report_deletion_capability
from retail_analytics.capabilities.reports import report_capabilities
from retail_analytics.capabilities.retrieval import retrieval_capability
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.metrics import default_catalog


def golden_applicability() -> tuple[str, dict[str, int]]:
    """Schema label and exact metric versions the running catalogs serve.

    Golden examples are delivered only when written for exactly these.
    """
    metrics = default_catalog()
    return (
        f"logical-catalog/{default_logical_catalog().version}",
        {m: metrics.latest_version(m) for m in sorted(metrics.metric_ids())},
    )


@dataclass(frozen=True)
class InvestigationServices:
    control: InvestigationControl
    launcher: InvestigationLauncher
    runtime: InvestigationRuntime
    inputs: PostgresInvestigationInputs
    principals: PostgresRunPrincipals
    tools: ToolRunner


def build_investigations(
    settings: BackendSettings,
    persistence: Persistence,
    access: AccessServices,
    scheduler: InvestigationScheduler,
    model: Model | Callable[[RunBudgets], Model],
    *,
    discovery: DiscoveryService | None = None,
    queries: QueryExecutionService | None = None,
    registry: CapabilityRegistry | None = None,
    artifacts: ArtifactServices | None = None,
    retriever: GoldenRetriever | None = None,
    exchange_rates: ExchangeRateProvider | None = None,
) -> InvestigationServices:
    """Wire the runtime and its permission-filtered tool catalog.

    Registered when their services are supplied: discovery (``discovery``),
    guarded queries (``queries``), Golden methods (``retriever``) and saved
    reports with deletion proposals (``artifacts``). Preferences and currency
    conversion are always registered; ``exchange_rates`` replaces the
    configured rate provider (offline runs). ``registry`` replaces the whole
    catalog (tests).
    """
    db = Database(persistence.engine)
    principals = PostgresRunPrincipals(db)
    inputs = PostgresInvestigationInputs(db)
    budgets = build_run_budgets(settings, persistence.budgets)
    evidence = build_evidence(persistence, settings=settings)
    preferences = build_preferences(persistence, access)
    context = build_context(persistence, access, evidence, preferences)
    if registry is None:
        specs: list[CapabilitySpec[Any, Any]] = (
            list(discovery_capabilities(discovery)) if discovery else []
        )
        if queries is not None:
            specs.append(
                analysis_capability(
                    executions=queries,
                    evidence=evidence,
                    principals=principals,
                    preferences=preferences,
                    budgets=budgets,
                    operations=persistence.tool_executions,
                )
            )
        specs.extend(
            preference_capabilities(
                preferences, principals=principals, inputs=inputs, gate=context.gate
            )
        )
        specs.append(
            currency_capability(
                build_currency_conversion(
                    settings, evidence, persistence.preferences, rates=exchange_rates
                )
            )
        )
        if retriever is not None:
            schema_version, metric_versions = golden_applicability()
            specs.append(
                retrieval_capability(
                    retriever,
                    schema_version=schema_version,
                    metric_versions=metric_versions,
                )
            )
        if artifacts is not None:
            reports = build_reports(
                persistence,
                artifacts.service,
                evidence,
                context.gate,
                access.resolver,
            )
            specs.extend(
                report_capabilities(reports, principals=principals, evidence=evidence)
            )
            specs.append(
                report_deletion_capability(
                    build_report_deletion(persistence, access.resolver)
                )
            )
        registry = CapabilityRegistry(specs)
    launcher = InvestigationLauncher(
        runs=persistence.runs,
        principals=principals,
        inputs=inputs,
        scheduler=scheduler,
        resolver=access.resolver,
    )
    runtime = InvestigationRuntime(
        runs=persistence.runs,
        principals=principals,
        inputs=inputs,
        resolver=access.resolver,
        budgets=budgets,
        context=context.builder,
        gate=context.gate,
        registry=registry,
        events=persistence.run_events,
        evidence=evidence,
        operations=persistence.tool_executions,
        queries=queries,
        launcher=launcher,
    )
    tools = ToolRunner(
        registry=registry,
        resolver=access.resolver,
        principals=principals,
        runs=persistence.runs,
        operations=persistence.tool_executions,
        budgets=budgets,
        progress=persistence.run_events,
    )
    control = InvestigationControl(
        resolver=access.resolver,
        guard=access.guard,
        sessions=persistence.sessions,
        runs=persistence.runs,
        principals=principals,
        inputs=inputs,
        events=persistence.run_events,
        scheduler=scheduler,
        launcher=launcher,
    )
    bind_runtime(runtime)
    # A plain model is budgeted as a whole; a factory (``bootstrap.models``)
    # budgets every provider attempt inside its retry/fallback chain.
    provider = (
        BudgetedModel(model, budgets) if isinstance(model, Model) else model(budgets)
    )
    bind_agent_services(AgentServices(runtime, tools, provider))
    return InvestigationServices(control, launcher, runtime, inputs, principals, tools)
