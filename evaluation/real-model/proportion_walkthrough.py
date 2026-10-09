"""Proportion walkthrough: does the work match the request with the real model?

Runs two short conversations through the ``agent_runtime`` target (real
investigation runtime, local backend, offline DuckDB over the frozen extract,
live provider chain), each in a fresh evaluation session:

- ``scalar``: "What's the latest revenue of September?", then "And August?",
  then "What was September's revenue again?" (a figure, a follow-up and a
  repeat that the evidence already answers);
- ``report``: a report request that should still give cited findings and
  recommended actions.

Prints, per run: tool sequence, provider, model requests, tokens, queries,
active and wall-clock seconds, and the SQL each query executed (from the
``query.compile`` span content). Also prints the reference monthly revenue
computed independently from the extract files (DuckDB, not the runtime).
``--show-answers`` prints the released answers for a reviewer. Run from the
repository root that holds the ``.env`` (see ``README.md`` here) against a
migrated PostgreSQL that no API holds; about 10-25 model requests.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import duckdb
from discovery_walkthrough import (
    EVALUATION,
    SCOPE_FROM,
    WalkthroughTarget,
    _span_tokens,
)

from retail_analytics.adapters.evaluation.files import load_manifest
from retail_analytics.adapters.evaluation.telemetry_recorder import (
    RecordingTelemetrySink,
)
from retail_analytics.application.contracts.evaluation import ScenarioInput, Turn
from retail_analytics.application.contracts.telemetry import (
    Attributes,
    CapturedPayload,
    PayloadSide,
)
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.bootstrap.agent_evaluation import realdata_source
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.domain.runs import ExecutionBackend

CONVERSATIONS = {
    "scalar": (
        "What's the latest revenue of September?",
        "And August?",
        "What was September's revenue again?",
    ),
    "report": (
        "Write a short report comparing August and September revenue, with "
        "recommended actions.",
    ),
}
COMPILE_SPAN = "query.compile"


class _SqlHandle:
    def __init__(self, sink: SqlRecordingSink, run_id: str | None) -> None:
        self._sink = sink
        self._run_id = run_id

    def set(self, attributes: Attributes) -> None:
        return None

    def event(self, name: str, attributes: Attributes) -> None:
        return None

    def fail(self, error_type: str) -> None:
        return None

    def payload(self, payload: CapturedPayload) -> None:
        content = payload.content
        if payload.side is PayloadSide.OUTPUTS and isinstance(content, dict):
            sql = content.get("executed_sql")
            if isinstance(sql, str):
                self._sink.add_sql(self._run_id, sql)


class SqlRecordingSink(RecordingTelemetrySink):
    """Records spans as usual, plus the executed SQL of each compiled query."""

    def __init__(self) -> None:
        super().__init__()
        self._sql: dict[str, list[str]] = {}
        self._sql_lock = threading.Lock()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        run_id: str | None,
        attributes: Attributes,
        start: datetime | None = None,
        root: bool = False,
    ) -> Iterator[Any]:
        if name != COMPILE_SPAN:
            with super().span(
                name, run_id=run_id, attributes=attributes, start=start, root=root
            ) as handle:
                yield handle
            return
        yield _SqlHandle(self, run_id)

    def add_sql(self, run_id: str | None, sql: str) -> None:
        with self._sql_lock:
            self._sql.setdefault(run_id or "?", []).append(sql)

    def sql(self, run_id: str) -> list[str]:
        with self._sql_lock:
            return list(self._sql.get(run_id, []))


def reference_revenue(product_scope: Sequence[str]) -> dict[str, float]:
    """Completed item sales per order month in scope, straight from the files."""
    lo, hi = (int(x) for x in product_scope[0].removeprefix("products:").split("-"))
    extract = EVALUATION / "realdata/extract"
    rows = duckdb.execute(
        """
        SELECT strftime(o.created_at, '%Y-%m') AS month,
               round(sum(i.sale_price), 2) AS revenue
        FROM read_csv_auto($items) AS i
        JOIN read_csv_auto($orders) AS o ON o.order_id = i.order_id
        WHERE i.status = 'Complete' AND i.product_id BETWEEN $lo AND $hi
        GROUP BY 1 ORDER BY 1
        """,
        {
            "items": str(extract / "order_items.csv.gz"),
            "orders": str(extract / "orders.csv.gz"),
            "lo": lo,
            "hi": hi,
        },
    ).fetchall()
    return {str(month): float(revenue) for month, revenue in rows}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=sorted(CONVERSATIONS), nargs="*")
    parser.add_argument("--show-answers", action="store_true")
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    settings = load_backend_settings()
    if settings.gemini_api_key is None:
        print("blocked: no Gemini key configured; nothing was run")
        return 3
    manifest = load_manifest(EVALUATION / "realdata/manifest.json")
    scope = next(s for s in manifest.scenarios if s.id == SCOPE_FROM).scope
    report: dict[str, Any] = {
        "reference_revenue": reference_revenue(scope.product_scope)
    }
    sink = SqlRecordingSink()
    with use_telemetry(Telemetry(sink, capture_content=True)):
        target = WalkthroughTarget(
            settings,
            realdata_source(EVALUATION),
            provider_chain(settings),
            turn_timeout=args.turn_timeout,
            backend=ExecutionBackend.LOCAL,
        )
        try:
            for name in args.only or list(CONVERSATIONS):
                target.runs, target.answers = [], []
                started = time.monotonic()
                target.run(
                    ScenarioInput(
                        scenario_id=f"proportion-{name}",
                        mode="fixture",
                        fixture_ref=None,
                        scope=scope,
                        dialogue=tuple(Turn(text=t) for t in CONVERSATIONS[name]),
                    )
                )
                runs = []
                for run in target.runs:
                    tokens = _span_tokens(sink, run.run_id)
                    runs.append(
                        {
                            "status": run.status,
                            "asked_a_question": run.asked,
                            "tools": run.tools,
                            "providers": tokens["providers"],
                            "model_requests": run.requests,
                            "tokens_budget": run.tokens,
                            "tokens_in": tokens["input"],
                            "tokens_out": tokens["output"],
                            "queries": run.queries,
                            "executed_sql": sink.sql(run.run_id),
                            "active_seconds": run.active_seconds,
                            "wall_seconds": run.wall_seconds,
                        }
                    )
                report[name] = {
                    "conversation_seconds": round(time.monotonic() - started, 1),
                    "runs": runs,
                }
                if args.show_answers:
                    for index, answer in enumerate(target.answers, 1):
                        print(f"--- {name} assistant message {index} ---\n{answer}\n")
        finally:
            target.close()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
