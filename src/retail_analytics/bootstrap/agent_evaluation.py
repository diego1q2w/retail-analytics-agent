"""Composition root of the ``agent_runtime`` evaluation target.

The target runs each scenario as real investigations: the same guarded model
steps, permission-filtered catalog, tool runner, compiler, privacy boundary,
evidence store, output gate and reports as the API/worker. Only two things are
replaced at this root:

- the warehouse: an offline ``FixtureWarehouse`` (held-out fixture or the
  frozen real-data extract) instead of BigQuery, with an ephemeral reference
  key so opaque references work without the operator's secret;
- optionally the model: a scripted plan (``adapters.models.scripted``) for
  deterministic runs, or the configured live provider chain.

Execution follows ``EXECUTION_BACKEND`` like the API: with
``local`` (the default) the investigations run on an in-process local manager
and need only PostgreSQL (migrated), e.g. the local stack from
``./scripts/bootstrap.sh``; with ``temporal`` they run as Temporal workflows
on a worker inside this process and also need
``TEMPORAL_ADDRESS``. The ``*_temporal`` factories select
Temporal explicitly whatever the setting. The backend is part of the
recorded target ID (``agent_runtime:local`` / ``agent_runtime:temporal``).
Each scenario gets a fresh evaluation executive, product entitlements and
session; nothing is shared with other executives. If the stack is unreachable
(or another local-execution process holds the database) every scenario is
blocked (``TargetUnavailable``), never passed.

Factories for ``retail-analytics-eval run --target``:

- ``...agent_evaluation:heldout_scripted`` / ``:realdata_scripted`` (offline,
  scripted plans in ``evaluation/agent-scripts``), and
  ``:heldout_scripted_temporal`` / ``:realdata_scripted_temporal``;
- ``...agent_evaluation:heldout_live`` / ``:realdata_live`` (live provider
  chain; counts against provider quotas).

A run with the scripted model measures the runtime and its guards under a
known plan, not a model's analytical quality. Report it as scripted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeVar

from pydantic import SecretStr
from pydantic_ai.models import Model

from retail_analytics.adapters.embedding.hashing import HashingEmbedder
from retail_analytics.adapters.evaluation.fixture_warehouse import (
    FixtureWarehouse,
    frozen_extract_warehouse,
    heldout_fixture_warehouse,
)
from retail_analytics.adapters.exchange_rates.fixture import FixtureRateProvider
from retail_analytics.adapters.local.investigations import LocalInvestigationManager
from retail_analytics.adapters.models.scripted import scripted_model
from retail_analytics.adapters.postgres.local_execution import ManagerLockHeld
from retail_analytics.application.authentication import (
    AuthenticationFailed,
    AuthFailure,
)
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.contracts.authentication import VerifiedToken
from retail_analytics.application.contracts.authorization import (
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.contracts.evaluation import (
    ConversationRecord,
    ObservedTable,
    ScenarioCanaries,
    ScenarioInput,
    TargetObservation,
    TargetUnavailable,
)
from retail_analytics.application.contracts.progress import EventKind
from retail_analytics.application.evaluation.agent_observation import (
    full_basket_totals,
    observe,
    scalar,
)
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.golden_seeding import (
    seed_golden_library,
    seed_principals,
)
from retail_analytics.application.reports import ReportService
from retail_analytics.bootstrap.access import AccessServices, build_access
from retail_analytics.bootstrap.artifacts import build_artifacts
from retail_analytics.bootstrap.budgets import build_run_budgets
from retail_analytics.bootstrap.config import BackendSettings, load_backend_settings
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.discovery import build_discovery
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.investigations import (
    InvestigationServices,
    build_investigations,
)
from retail_analytics.bootstrap.knowledge import build_knowledge
from retail_analytics.bootstrap.local_investigations import (
    build_local_investigations,
)
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.bootstrap.persistence import (
    Persistence,
    persistence_from_settings,
)
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.bootstrap.query import build_query_execution
from retail_analytics.bootstrap.reports import build_reports
from retail_analytics.bootstrap.retrieval import build_retrieval
from retail_analytics.domain.access import Role, permissions_for
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.evidence import scope_digest
from retail_analytics.domain.runs import ExecutionBackend, RunStatus

if TYPE_CHECKING:
    from temporalio.client import Client

TARGET_ID: Final = "agent_runtime"
EVALUATION_DIR: Final = Path("evaluation")
HELDOUT_FIXTURE: Final = EVALUATION_DIR / "heldout" / "fixture"
REALDATA_EXTRACT: Final = EVALUATION_DIR / "realdata" / "extract"
SCRIPTS_DIR: Final = EVALUATION_DIR / "agent-scripts"
EVALUATION_PROJECT: Final = "fixture-evaluation"
# Keys opaque references over the synthetic or pseudonymized evaluation data
# only. Fixed so references, and therefore results, are reproducible; it
# never keys real customer data.
EVALUATION_REFERENCE_KEY: Final = "evaluation-only-reference-key-for-fixture-data"
_TERMINAL_WAIT = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.WAITING_FOR_INPUT,
    }
)
_PERMISSIONS = frozenset(p.value for p in permissions_for(frozenset({Role.EXECUTIVE})))

T = TypeVar("T")
type ModelSource = Model | Callable[[RunBudgets], Model]


class _NoTokens:
    """The target never authenticates tokens; principals are provisioned."""

    def verify(self, token: str) -> VerifiedToken:
        raise AuthenticationFailed(AuthFailure.MALFORMED)


@dataclass(frozen=True)
class FixtureSource:
    """What the target answers from, and its never-release canaries."""

    warehouse: FixtureWarehouse
    canaries: Callable[[frozenset[str]], ScenarioCanaries]
    products: Callable[[Sequence[str]], frozenset[str]]


def evaluation_executive_id(scenario_id: str) -> str:
    return "eval-" + hashlib.sha256(scenario_id.encode()).hexdigest()[:16]


def expand_scope(spec: Sequence[str]) -> frozenset[str]:
    """Product IDs from manifest scopes: plain IDs or ``products:<lo>-<hi>``."""
    products: set[str] = set()
    for item in spec:
        if item.startswith("products:"):
            low, _, high = item.removeprefix("products:").partition("-")
            products.update(str(p) for p in range(int(low), int(high) + 1))
        else:
            products.add(item)
    return frozenset(products)


def heldout_source(root: Path = EVALUATION_DIR) -> FixtureSource:
    fixture = root / "heldout" / "fixture"
    warehouse = heldout_fixture_warehouse(fixture)
    customers = json.loads((fixture / "customers.json").read_text(encoding="utf-8"))
    products = json.loads((fixture / "products.json").read_text(encoding="utf-8"))
    orders = json.loads((fixture / "orders.json").read_text(encoding="utf-8"))

    def canaries(scope: frozenset[str]) -> ScenarioCanaries:
        baskets = {
            str(o["order_id"]): [
                (str(i["product_id"]), float(i["sale_price"])) for i in o["items"]
            ]
            for o in orders
        }
        complete = {
            str(o["order_id"]): [
                (str(i["product_id"]), float(i["sale_price"]))
                for i in o["items"]
                if i["status"] == "Complete"
            ]
            for o in orders
        }
        outside = [p for p in products if str(p["product_id"]) not in scope]
        return ScenarioCanaries(
            personal_strings=frozenset(
                s
                for c in customers
                for s in (c["first_name"], c["last_name"], c["email"])
            ),
            raw_customer_ids=frozenset(str(c["customer_id"]) for c in customers),
            exact_ages=frozenset(int(c["age"]) for c in customers),
            out_of_scope_products=frozenset(p["name"] for p in outside),
            out_of_scope_product_ids=frozenset(str(p["product_id"]) for p in outside),
            full_basket_totals=full_basket_totals(baskets, scope)
            | full_basket_totals(complete, scope),
        )

    return FixtureSource(warehouse, canaries, expand_scope)


def realdata_source(root: Path = EVALUATION_DIR) -> FixtureSource:
    """The frozen extract. Its identifiers are dense pseudonyms, so raw-ID
    canaries do not apply; out-of-scope products are checked by ID and by the
    names no permitted product shares."""
    manifest = json.loads(
        (root / "realdata" / "extract" / "extract-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    warehouse = frozen_extract_warehouse(
        root / "realdata" / "extract", str(manifest["data_ref"])
    )
    names = warehouse.product_names()

    def canaries(scope: frozenset[str]) -> ScenarioCanaries:
        inside = {n for p, n in names if p in scope}
        return ScenarioCanaries(
            out_of_scope_products=frozenset(
                n for p, n in names if p not in scope and n not in inside
            ),
            out_of_scope_product_ids=frozenset(p for p, _ in names if p not in scope),
        )

    return FixtureSource(warehouse, canaries, expand_scope)


def load_plans(name: str, root: Path = SCRIPTS_DIR) -> dict[str, list[Any]]:
    data = json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
    plans: dict[str, list[Any]] = data["plans"]
    return plans


class _Loop:
    """A private event loop thread: the runner's target protocol is sync."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coroutine: Coroutine[Any, Any, T], timeout: float) -> T:
        future: Future[T] = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


@dataclass
class _Harness:
    persistence: Persistence
    access: AccessServices
    services: InvestigationServices
    reports: ReportService
    evidence: EvidenceService
    # Temporal: the worker task. Local: the opened in-process manager.
    worker_task: asyncio.Task[None] | None
    manager: LocalInvestigationManager | None = None


@dataclass
class AgentRuntimeTarget:
    """``EvaluationTarget`` running scenarios through the investigation runtime.

    ``backend`` defaults to the settings' execution backend; ``target_id``
    defaults to ``agent_runtime:<backend>`` so results record it.
    """

    settings: BackendSettings
    source: FixtureSource
    model: ModelSource
    turn_timeout: float = 240.0
    seed_knowledge: bool = True
    target_id: str = ""
    backend: ExecutionBackend | None = None
    _loop: _Loop | None = field(default=None, init=False, repr=False)
    _harness: _Harness | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = self.settings.execution_backend
        if not self.target_id:
            self.target_id = f"{TARGET_ID}:{self.backend.value}"

    def run(self, case: ScenarioInput) -> TargetObservation:
        if self._loop is None:
            self._loop = _Loop()
        budget = self.turn_timeout * (len(case.dialogue) + 1) + 120
        return self._loop.run(self._run(case), timeout=budget)

    def close(self) -> None:
        if self._loop is None:
            return
        if self._harness is not None:
            self._loop.run(self._shutdown(), timeout=60)
        self._loop.stop()
        self._loop = None

    async def _shutdown(self) -> None:
        harness = self._harness
        if harness is None:
            return
        if harness.manager is not None:
            await harness.manager.close()
        if harness.worker_task is not None:
            harness.worker_task.cancel()
            # Worker shutdown is best effort; its cancellation is expected.
            with contextlib.suppress(BaseException):
                await harness.worker_task
        harness.persistence.close()
        self._harness = None

    async def _start(self) -> _Harness:
        if self._harness is not None:
            return self._harness
        settings = self.settings
        local_backend = self.backend is ExecutionBackend.LOCAL
        if settings.database_url is None or (
            settings.temporal_address is None and not local_backend
        ):
            raise TargetUnavailable
        client: Client | None = None
        try:
            if not local_backend:
                # Imported only for the Temporal backend.
                from retail_analytics.bootstrap.temporal import connect

                client = await connect(
                    settings.temporal_address or "", settings.temporal_namespace
                )
            persistence = persistence_from_settings(settings)
        except Exception:
            raise TargetUnavailable from None
        access = build_access(persistence, _NoTokens())
        # The evaluation warehouse needs no BigQuery project; references use
        # a key that exists only for this process.
        local = settings.model_copy(
            update={
                "bigquery_project": EVALUATION_PROJECT,
                "reference_key": SecretStr(EVALUATION_REFERENCE_KEY),
            }
        )
        warehouse = self.source.warehouse
        discovery = build_discovery(local, provider=warehouse)
        budgets = build_run_budgets(local, persistence.budgets)
        queries = build_query_execution(
            local,
            persistence,
            access.resolver,
            discovery,
            warehouse=warehouse,
            budgets=budgets,
        )
        artifacts = build_artifacts(local, persistence)
        knowledge = build_knowledge(persistence, artifacts, access.resolver)
        if self.seed_knowledge:
            await _seed_if_empty(persistence, knowledge)
        retriever = build_retrieval(local, knowledge, embedder=HashingEmbedder())
        wiring: dict[str, Any] = {
            "discovery": discovery,
            "queries": queries,
            "artifacts": artifacts,
            "retriever": retriever,
            "exchange_rates": FixtureRateProvider({}),
        }
        task: asyncio.Task[None] | None = None
        manager: LocalInvestigationManager | None = None
        if client is None:
            built = build_local_investigations(
                local, persistence, access, self.model, **wiring
            )
            services, manager = built.services, built.manager
            try:
                await manager.open()
            except ManagerLockHeld:
                # Another local-execution process (an API) owns the database.
                persistence.close()
                raise TargetUnavailable from None
        else:
            from retail_analytics.bootstrap.temporal import (
                investigation_worker,
            )
            from retail_analytics.bootstrap.temporal import (
                scheduler as temporal_scheduler,
            )

            queue = "eval-" + uuid.uuid4().hex
            services = build_investigations(
                local,
                persistence,
                access,
                temporal_scheduler(client, queue),
                self.model,
                **wiring,
            )
            worker = investigation_worker(client, queue, services)
            task = asyncio.get_running_loop().create_task(worker.run())
        evidence = build_evidence(persistence, settings=local)
        context = build_context(
            persistence, access, evidence, build_preferences(persistence, access)
        )
        reports = build_reports(
            persistence, artifacts.service, evidence, context.gate, access.resolver
        )
        self._harness = _Harness(
            persistence, access, services, reports, evidence, task, manager
        )
        return self._harness

    async def _run(self, case: ScenarioInput) -> TargetObservation:
        harness = await self._start()
        db = harness.persistence
        products = self.source.products(case.scope.product_scope)
        principal, session_id = await self.provision(case, products)
        control = harness.services.control
        started = time.monotonic()
        run_ids: list[str] = []
        for turn in case.dialogue:
            waiting = run_ids[-1] if run_ids else None
            question = (
                None
                if waiting is None
                else await harness.services.inputs.open_question(waiting)
            )
            if question is not None and waiting is not None:
                await control.answer(
                    principal,
                    run_id=waiting,
                    question_id=question.question_id,
                    text=turn.text,
                    submission_key=uuid.uuid4().hex,
                )
                await self._wait(db, waiting, after_answer=True)
                continue
            handle = await control.start(
                principal,
                session_id=session_id,
                text=turn.text,
                submission_key=uuid.uuid4().hex,
            )
            run_ids.append(handle.run_id)
            await self._wait(db, handle.run_id)
        elapsed = time.monotonic() - started
        record = await self._record(harness, principal, session_id, run_ids)
        record = ConversationRecord(
            answers=record.answers,
            tables=record.tables,
            tool_calls=record.tool_calls,
            report_texts=record.report_texts,
            report_actions=record.report_actions,
            report_evidence=record.report_evidence,
            measurements={**record.measurements, "latency_seconds": elapsed},
            user_texts=tuple(turn.text for turn in case.dialogue),
        )
        return observe(record, self.source.canaries(products))

    async def provision(
        self, case: ScenarioInput, products: frozenset[str]
    ) -> tuple[Principal, str]:
        """The scenario's executive (stable per scenario) and a new session.

        Opaque references are keyed per executive, so a stable executive keeps
        answers reproducible between runs.
        """
        db = (await self._start()).persistence
        executive_id = evaluation_executive_id(case.scenario_id)
        with contextlib.suppress(Exception):  # registered by an earlier run
            await db.access_admin.register_executive(
                ExecutiveRegistration(
                    executive_id=executive_id,
                    issuer="evaluation",
                    subject=executive_id,
                    roles=frozenset({Role.EXECUTIVE}),
                    label=f"Evaluation {case.scope.executive_ref}",
                )
            )
        await db.access_admin.replace_products(executive_id, set(products))
        session = await db.sessions.create_session(
            "ses-" + uuid.uuid4().hex, executive_id
        )
        return Principal(executive_id, _PERMISSIONS), session.session_id

    async def _wait(
        self, db: Persistence, run_id: str, *, after_answer: bool = False
    ) -> None:
        deadline = time.monotonic() + self.turn_timeout
        # After an answer the run leaves WAITING_FOR_INPUT before finishing.
        settle = 1.0 if after_answer else 0.0
        while time.monotonic() < deadline:
            run = await db.runs.get_run(run_id)
            if (
                run is not None
                and run.status in _TERMINAL_WAIT
                and (settle <= 0 or run.status is not RunStatus.WAITING_FOR_INPUT)
            ):
                return
            await asyncio.sleep(0.2)
            settle -= 0.2
        raise TimeoutError("investigation did not finish in time")

    async def _record(
        self,
        harness: _Harness,
        principal: Principal,
        session_id: str,
        run_ids: Sequence[str],
    ) -> ConversationRecord:
        db = harness.persistence
        messages = await db.sessions.recent_messages(session_id, 500)
        answers = tuple(m.content for m in messages if m.role is MessageRole.ASSISTANT)
        tool_calls: list[str] = []
        requests = 0.0
        queries = 0.0
        for run_id in run_ids:
            for event in await db.run_events.replay(run_id, limit=10_000):
                if event.tool is not None and event.kind is EventKind.TOOL_STARTED:
                    tool_calls.append(event.tool.capability)
            budget = await db.budgets.get(run_id)
            if budget is not None:
                requests += budget.usage.provider_requests
                queries += budget.usage.queries
            for op in await db.tool_executions.for_run(run_id):
                tool_calls.append(op.capability)
        tables = await self._tables(harness, principal, run_ids)
        texts: list[str] = []
        actions = 0
        cited = 0
        # A report that cannot be read is simply not observed.
        with contextlib.suppress(Exception):
            listings = await harness.reports.list_reports(
                principal, session_id=session_id, limit=100
            )
            for listing in listings:
                document = await harness.reports.read(principal, listing.report_id)
                texts.append(document.markdown)
                actions += _action_count(document.markdown)
                cited += len(document.evidence)
        return ConversationRecord(
            answers=answers,
            tables=tables,
            tool_calls=tuple(tool_calls),
            report_texts=tuple(texts),
            report_actions=actions,
            report_evidence=cited,
            measurements={"model_requests": requests, "queries": queries},
        )

    async def _tables(
        self, harness: _Harness, principal: Principal, run_ids: Sequence[str]
    ) -> tuple[ObservedTable, ...]:
        if not run_ids:
            return ()
        try:
            context = await harness.access.resolver.context_for_run(
                principal, run_ids[-1]
            )
            session = await harness.evidence.session_standing(context, run_ids=run_ids)
        except Exception:  # no usable evidence means none is observed
            return ()
        # The session is the scenario's own: every record in it is observed,
        # including records no longer usable under current authority.
        standing = [s.evidence for s in session.standings]
        expected = scope_digest(context.product_scope)
        return tuple(
            ObservedTable(
                evidence_id=e.evidence_id,
                columns=e.content.table.column_names,
                roles=tuple(c.role for c in e.content.table.columns),
                sources=tuple(c.sources for c in e.content.table.columns),
                rows=tuple(
                    tuple(scalar(cell) for cell in row) for row in e.content.table.rows
                ),
                truncated=e.content.table.truncated,
                scope_matches=e.authority.scope_digest == expected,
            )
            for e in reversed(standing)
        )


def _action_count(markdown: str) -> int:
    lines = markdown.splitlines()
    try:
        start = lines.index("## Recommended actions")
    except ValueError:
        return 0
    count = 0
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if (
            line.lstrip().startswith(("- ", "* ", "1. "))
            or line[:3].rstrip(".").isdigit()
        ):
            count += 1
    return count


async def _seed_if_empty(persistence: Persistence, knowledge: Any) -> None:
    """Publish the Golden seeds once per database, by evaluation principals."""
    if await knowledge.index_source.published_documents(None, 1):
        return
    author_id = "eval-golden-author"
    reviewer_id = "eval-golden-reviewer"
    for executive_id, role in (
        (author_id, Role.EXECUTIVE),
        (reviewer_id, Role.REVIEWER),
    ):
        with contextlib.suppress(Exception):  # already registered
            await persistence.access_admin.register_executive(
                ExecutiveRegistration(
                    executive_id=executive_id,
                    issuer="evaluation",
                    subject=executive_id,
                    roles=frozenset({role}),
                    label="Evaluation seed principal",
                )
            )
    author, reviewer = seed_principals(author_id, reviewer_id)
    await seed_golden_library(knowledge.service, author, reviewer)


# Factories (``--target retail_analytics.bootstrap.agent_evaluation:<name>``)


def _settings() -> BackendSettings:
    try:
        return load_backend_settings()
    except Exception:
        raise TargetUnavailable from None


def heldout_scripted() -> AgentRuntimeTarget:
    return AgentRuntimeTarget(
        _settings(), heldout_source(), scripted_model(load_plans("heldout"))
    )


def realdata_scripted() -> AgentRuntimeTarget:
    return AgentRuntimeTarget(
        _settings(), realdata_source(), scripted_model(load_plans("realdata"))
    )


def heldout_scripted_temporal() -> AgentRuntimeTarget:
    return AgentRuntimeTarget(
        _settings(),
        heldout_source(),
        scripted_model(load_plans("heldout")),
        backend=ExecutionBackend.TEMPORAL,
    )


def realdata_scripted_temporal() -> AgentRuntimeTarget:
    return AgentRuntimeTarget(
        _settings(),
        realdata_source(),
        scripted_model(load_plans("realdata")),
        backend=ExecutionBackend.TEMPORAL,
    )


def heldout_live() -> AgentRuntimeTarget:
    settings = _settings()
    return AgentRuntimeTarget(settings, heldout_source(), provider_chain(settings))


def realdata_live() -> AgentRuntimeTarget:
    settings = _settings()
    return AgentRuntimeTarget(settings, realdata_source(), provider_chain(settings))


__all__ = [
    "TARGET_ID",
    "AgentRuntimeTarget",
    "FixtureSource",
    "evaluation_executive_id",
    "expand_scope",
    "heldout_live",
    "heldout_scripted",
    "heldout_scripted_temporal",
    "heldout_source",
    "load_plans",
    "realdata_live",
    "realdata_scripted",
    "realdata_scripted_temporal",
    "realdata_source",
]
