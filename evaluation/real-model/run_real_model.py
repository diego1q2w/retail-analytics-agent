"""Run the compact real-model evaluation (T37) and write its sanitized results.

Composition only; the logic lives in ``application.evaluation.real_model``.
Each selected scenario runs through the ``agent_runtime`` target (real
investigation runtime on the default LOCAL execution backend, offline DuckDB
warehouse over the frozen extract or the held-out fixture) with the configured
live provider chain (Gemini primary, GPT fallback when configured). An
in-process telemetry recorder attributes every run to the provider that
actually answered.

Writes ``results.json`` (no answer text), ``SUMMARY.md`` and one transcript per
conversation for the human review (pseudonymized or synthetic data only;
refused if it looks like PII or a secret). Run from the repository root that
holds the ``.env`` with the provider keys (see ``README.md`` here).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from retail_analytics.adapters.evaluation.files import load_manifest
from retail_analytics.adapters.evaluation.telemetry_recorder import (
    RecordingTelemetrySink,
)
from retail_analytics.adapters.models.gemini_interactions import PROVIDER
from retail_analytics.application.contracts.evaluation import (
    ConversationOutcome,
    ConversationRecord,
    ObservedTable,
    RealModelEvaluation,
    ScenarioInput,
    TargetObservation,
)
from retail_analytics.application.evaluation.agent_observation import observe
from retail_analytics.application.evaluation.manifest import Scenario
from retail_analytics.application.evaluation.real_model import (
    figure_checks,
    provider_use,
    render_summary,
)
from retail_analytics.application.evaluation.results import assert_no_sensitive
from retail_analytics.application.evaluation.runner import RunConfig, run_scenario
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.bootstrap.agent_evaluation import (
    AgentRuntimeTarget,
    FixtureSource,
    heldout_source,
    realdata_source,
)
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.bootstrap.realdata_benchmark import verify
from retail_analytics.domain.runs import ExecutionBackend

HERE = Path(__file__).resolve().parent
EVALUATION = HERE.parent
# Chosen before any run (not tuned on outcomes): customer, product, time,
# demographic analysis, follow-ups and two reports with next steps from the
# frozen extract; a schema question, a scoped definition correction and a
# direct-identifier request from the held-out fixture.
DEFAULT_SELECTION = (
    "realdata:rd-l1-revenue-q3-customers",
    "realdata:rd-l1-state-top3-q4",
    "realdata:rd-l1-product-top3-q4",
    "realdata:rd-l1-age-band-spend-q4",
    "realdata:rd-l2-monthly-trend-h2",
    "realdata:rd-l2-customer-concentration-report",
    "realdata:rd-l2-category-change-report",
    "heldout:ho-l1-source-schema-question",
    "heldout:ho-l2-definition-correction",
    "heldout:ho-l1-pii-names-emails",
)
SAFETY_FLAGS = (
    "pii_released",
    "raw_customer_id_released",
    "exact_age_released",
    "out_of_scope_data_released",
    "full_basket_data_released",
    "raw_table_query_executed",
)


@dataclass
class _Captured:
    record: ConversationRecord
    run_ids: tuple[str, ...]
    statuses: tuple[str, ...]


@dataclass
class CapturingTarget(AgentRuntimeTarget):
    """The agent_runtime target, keeping each conversation's durable record."""

    captured: dict[str, _Captured] = field(default_factory=dict)
    _current: str = ""

    def run(self, case: ScenarioInput) -> TargetObservation:
        self._current = case.scenario_id
        return super().run(case)

    async def _record(
        self, harness: Any, principal: Any, session_id: str, run_ids: Sequence[str]
    ) -> ConversationRecord:
        record = await super()._record(harness, principal, session_id, run_ids)
        statuses = []
        for run_id in run_ids:
            run = await harness.persistence.runs.get_run(run_id)
            statuses.append(run.status.value if run is not None else "missing")
        self.captured[self._current] = _Captured(
            record, tuple(run_ids), tuple(statuses)
        )
        return record


def _transcript(scenario: Scenario, record: ConversationRecord) -> str:
    lines = [f"# {scenario.id}: {scenario.title}", ""]
    for index, turn in enumerate(scenario.dialogue, 1):
        lines += [f"## User turn {index}", "", turn.text, ""]
    for index, answer in enumerate(record.answers, 1):
        lines += [f"## Assistant message {index}", "", answer.strip(), ""]
    for index, report in enumerate(record.report_texts, 1):
        lines += [f"## Saved report {index}", "", report.strip(), ""]
    if not record.report_texts:
        lines += ["_No report was saved in this conversation._", ""]
    # Trailing spaces (Markdown hard breaks) are dropped to keep diffs clean.
    return "\n".join(line.rstrip() for line in lines)


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(HERE), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _code_version() -> str:
    """Short commit, marked ``+dirty`` when the working tree has changes."""
    with contextlib.suppress(Exception):
        dirty = "+dirty" if _git("status", "--porcelain", "--", "src") else ""
        return _git("rev-parse", "--short", "HEAD") + dirty
    return "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--select", nargs="*", default=list(DEFAULT_SELECTION))
    # suite:scenario items, run grouped by suite in the given order
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    problems = verify(EVALUATION / "realdata")
    if problems:  # a corrupt or mismatched frozen extract blocks the run
        print("frozen extract check failed:", *problems, sep="\n  ")
        return 3
    settings = load_backend_settings()
    if settings.gemini_api_key is None:
        print("blocked: no Gemini key configured; nothing was run")
        return 3
    suites: dict[str, tuple[Path, FixtureSource]] = {
        "realdata": (
            EVALUATION / "realdata/manifest.json",
            realdata_source(EVALUATION),
        ),
        "heldout": (EVALUATION / "heldout/manifest.json", heldout_source(EVALUATION)),
    }
    config = RunConfig(
        mode="fixture",
        available_capabilities=frozenset({"agent_runtime", "frozen_extract_source"}),
    )
    out: Path = args.out
    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    recorder = RecordingTelemetrySink()
    outcomes: list[ConversationOutcome] = []
    datasets: dict[str, str] = {}
    targets: dict[str, CapturingTarget] = {}
    target_id = ""
    with use_telemetry(Telemetry(recorder)):
        try:
            order = list(dict.fromkeys(i.partition(":")[0] for i in args.select))
            selected = sorted(
                args.select, key=lambda i: order.index(i.partition(":")[0])
            )
            for item in selected:
                suite, _, scenario_id = item.partition(":")
                manifest_path, source = suites[suite]
                manifest = load_manifest(manifest_path)
                datasets[manifest.manifest_id] = manifest.manifest_version
                scenario = next(s for s in manifest.scenarios if s.id == scenario_id)
                target = targets.get(suite)
                if target is None:
                    # One local manager at a time: it holds a database lock.
                    for previous in targets.values():
                        previous.close()
                    target = CapturingTarget(
                        settings,
                        source,
                        provider_chain(settings),
                        turn_timeout=args.turn_timeout,
                        backend=ExecutionBackend.LOCAL,
                    )
                    targets[suite] = target
                target_id = target.target_id
                print(f"running {suite}:{scenario_id} ...", flush=True)
                case = run_scenario(scenario, config, target)
                captured = target.captured.pop(scenario_id, None)
                record = captured.record if captured else None
                tables: tuple[ObservedTable, ...] = record.tables if record else ()
                released = (
                    "\n".join((*record.answers, *record.report_texts)) if record else ""
                )
                transcript = None
                if record is not None:
                    text = _transcript(scenario, record)
                    assert_no_sensitive(text)
                    transcript = f"transcripts/{scenario_id}.md"
                    (out / transcript).write_text(text, encoding="utf-8")
                flags = (
                    observe(
                        record,
                        source.canaries(source.products(scenario.scope.product_scope)),
                    ).values
                    if record
                    else {}
                )
                outcome = ConversationOutcome(
                    scenario_id=scenario.id,
                    suite=manifest.manifest_id,
                    category=scenario.category,
                    level=scenario.level,
                    judge_required=scenario.judge is not None,
                    turns=len(scenario.dialogue),
                    run_statuses=captured.statuses if captured else (),
                    runner_status=case.status,
                    runner_reason=case.reason,
                    error_type=case.error_type,
                    strict_checks_passed=sum(c.passed for c in case.checks),
                    strict_checks_total=len(case.checks),
                    figures=figure_checks(scenario, released, tables),
                    safety_flags={
                        flag: bool(flags[flag])
                        for flag in SAFETY_FLAGS
                        if flag in flags
                    },
                    provider=provider_use(
                        recorder.spans(),
                        captured.run_ids if captured else (),
                        PROVIDER,
                    ),
                    measurements={m.name: m.value for m in case.measurements},
                    transcript=transcript,
                )
                print(
                    f"  {case.status} provider={outcome.provider.label} "
                    f"runs={','.join(outcome.run_statuses)}",
                    flush=True,
                )
                outcomes.append(outcome)
        finally:
            for target in targets.values():
                target.close()
    extract = json.loads(
        (EVALUATION / "realdata/extract/extract-manifest.json").read_text("utf-8")
    )
    datasets["frozen_extract_digest"] = str(extract["extract_digest"])
    datasets["heldout_fixture"] = "heldout-fixture-1"
    result = RealModelEvaluation(
        recorded_on=datetime.now(UTC).date().isoformat(),
        code_version=_code_version(),
        target_id=target_id,
        execution_backend=ExecutionBackend.LOCAL.value,
        primary_provider=PROVIDER,
        configured_models={
            PROVIDER: settings.agent_gemini_model,
            **(
                {"openai": settings.agent_openai_model}
                if settings.openai_api_key is not None
                else {}
            ),
        },
        datasets=datasets,
        conversations=tuple(outcomes),
    )
    text = json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True)
    assert_no_sensitive(text)
    (out / "results.json").write_text(text + "\n", encoding="utf-8")
    (out / "SUMMARY.md").write_text(render_summary(result), encoding="utf-8")
    print(render_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
