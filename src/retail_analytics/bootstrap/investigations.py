"""Composition of guarded investigation services and the durable agent."""

from __future__ import annotations

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
from retail_analytics.application.discovery import DiscoveryService
from retail_analytics.application.investigation_runtime import InvestigationRuntime
from retail_analytics.application.investigations import (
    InvestigationControl,
    InvestigationLauncher,
)
from retail_analytics.application.ports.investigations import InvestigationScheduler
from retail_analytics.application.query_execution import QueryExecutionService
from retail_analytics.application.tool_runner import ToolRunner
from retail_analytics.application.tools import CapabilityRegistry, CapabilitySpec
from retail_analytics.bootstrap.access import AccessServices
from retail_analytics.bootstrap.budgets import build_run_budgets
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.capabilities.analysis import analysis_capability
from retail_analytics.capabilities.discovery import discovery_capabilities


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
    model: Model,
    *,
    discovery: DiscoveryService | None = None,
    queries: QueryExecutionService | None = None,
    registry: CapabilityRegistry | None = None,
) -> InvestigationServices:
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
    bind_agent_services(AgentServices(runtime, tools, BudgetedModel(model, budgets)))
    return InvestigationServices(control, launcher, runtime, inputs, principals, tools)
