"""Bounded live smoke: the shipped local default over HTTP, real BigQuery.

Drives one short conversation through a running API (local execution, live
mode) with a dev token, then:

- reads each run's MLflow trace (``tr-`` + id derived from the run id) and
  records model attempts (provider, model, outcome, tokens), the tool
  sequence, BigQuery jobs and bytes, context restarts, and whether the
  sanitized prompt, tool arguments and SQL are visible on the spans;
- computes the expected monthly revenue independently with its own BigQuery
  SQL (completed items, ``orders.created_at``, every product, the local
  administrator's scope) and checks the released answers state it.

Live data changes daily, so the reference is computed right after the
conversation and recorded with its time; it is never a frozen expectation.
Writes ``<out>/<label>.json`` and ``<out>/transcripts/<label>.md``. Never
prints the token. Example (an API on a throwaway database, see ``README.md``):
``python evaluation/real-model/live_smoke.py --api http://127.0.0.1:18139
--token-file /path/token``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from retail_analytics.adapters.evaluation.mlflow_traces import trace_facts
from retail_analytics.application.evaluation.real_model import number_in_text
from retail_analytics.application.evaluation.results import assert_no_sensitive
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.bootstrap.trace_lookup import mlflow_base, trace_ref

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "efficiency" / "results"
TURNS = ("What's the latest revenue of September?", "And August?")
# Month (YYYY-MM) each turn is expected to answer; the reference SQL computes it.
MONTHS = ("2026-09", "2026-08")
TERMINAL = {"completed", "partial", "failed", "cancelled"}
REFERENCE_SQL = """
SELECT FORMAT_TIMESTAMP('%Y-%m', o.created_at) AS month,
       ROUND(SUM(oi.sale_price), 2) AS revenue
FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
JOIN `bigquery-public-data.thelook_ecommerce.orders` AS o
  ON o.order_id = oi.order_id
WHERE oi.status = 'Complete'
  AND o.created_at >= TIMESTAMP(@start) AND o.created_at < TIMESTAMP(@end)
GROUP BY month ORDER BY month
"""


def _drive(
    client: httpx.Client, turns: Sequence[str], timeout: float
) -> tuple[str, list[dict[str, Any]]]:
    session = client.post("/v1/sessions", json={"submission_key": uuid.uuid4().hex})
    session.raise_for_status()
    session_id = str(session.json()["session_id"])
    runs = []
    for text in turns:
        started = time.monotonic()
        response = client.post(
            f"/v1/sessions/{session_id}/runs",
            json={"text": text, "submission_key": uuid.uuid4().hex},
        )
        response.raise_for_status()
        run_id = str(response.json()["run_id"])
        detail: dict[str, Any] = {}
        while time.monotonic() - started < timeout:
            detail = client.get(f"/v1/runs/{run_id}").json()
            if detail.get("status") in TERMINAL:
                break
            if detail.get("status") == "waiting_for_input":
                # The smoke does not answer questions: record and stop the run.
                client.post(f"/v1/runs/{run_id}/cancel")
            time.sleep(1.0)
        answer = detail.get("answer") or {}
        question = detail.get("question") or {}
        runs.append(
            {
                "run_id": run_id,
                "trace_id": trace_ref(run_id),
                "status": detail.get("status", "timeout"),
                "client_seconds": round(time.monotonic() - started, 1),
                "answer": answer.get("text", ""),
                "withheld": bool(answer.get("withheld", False)),
                "question": (question.get("text") or {}).get("text"),
            }
        )
    return session_id, runs


def _reload(client: httpx.Client, saved: Mapping[str, Any]) -> dict[str, Any]:
    detail = client.get(f"/v1/runs/{saved['run_id']}").json()
    answer = detail.get("answer") or {}
    keep = ("run_id", "trace_id", "status", "client_seconds", "question")
    return {
        **{k: saved.get(k) for k in keep},
        "answer": answer.get("text", ""),
        "withheld": bool(answer.get("withheld", False)),
    }


def _spans(base: str, run_id: str, wait: float) -> list[Mapping[str, Any]]:
    deadline = time.monotonic() + wait
    while True:
        try:
            response = httpx.get(
                f"{base}/api/3.0/mlflow/traces/batchGet",
                params={"trace_ids": trace_ref(run_id)},
                timeout=10,
            )
            traces = response.json().get("traces", [])
            spans = traces[0].get("spans", []) if traces else []
            if any(s.get("name") == "investigation.run" for s in spans):
                return list(spans)
        except (httpx.HTTPError, ValueError):
            spans = []
        if time.monotonic() > deadline:
            return list(spans)
        time.sleep(2.0)


def reference(months: Sequence[str]) -> dict[str, float]:
    """Completed revenue per month, computed independently on BigQuery."""
    from google.cloud import bigquery

    from retail_analytics.adapters.google_access import create_bigquery_client

    settings = load_backend_settings()
    if settings.bigquery_project is None:
        raise SystemExit("blocked: GOOGLE_CLOUD_PROJECT is not set")
    client = create_bigquery_client(
        settings.bigquery_project, settings.bigquery_location
    )
    first = min(months) + "-01"
    last = max(months)
    year, month = int(last[:4]), int(last[5:])
    end = f"{year + month // 12}-{month % 12 + 1:02d}-01"
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start", "STRING", first),
            bigquery.ScalarQueryParameter("end", "STRING", end),
        ],
        maximum_bytes_billed=1024**3,
    )
    rows = client.query(REFERENCE_SQL, job_config=config).result()
    return {str(r["month"]): float(r["revenue"]) for r in rows}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--label", default="live-smoke")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--revision", default="unknown")
    parser.add_argument("--turn-timeout", type=float, default=300.0)
    parser.add_argument(
        "--rescan",
        action="store_true",
        help="re-read answers and traces of the runs in <out>/<label>.json; "
        "starts no conversation and recomputes no reference",
    )
    args = parser.parse_args(argv)
    token = args.token_file.read_text("utf-8").strip()
    settings = load_backend_settings()
    base = mlflow_base(settings.telemetry_traces_endpoint)
    headers = {"Authorization": f"Bearer {token}"}
    started = datetime.now(UTC).isoformat(timespec="seconds")
    with httpx.Client(base_url=args.api, headers=headers, timeout=30) as client:
        health = httpx.get(f"{args.api}/healthz", timeout=10).json()
        if args.rescan:
            saved = json.loads((args.out / f"{args.label}.json").read_text("utf-8"))
            session_id, started = saved["session_id"], saved["started_at"]
            runs = [_reload(client, r) for r in saved["runs"]]
            refs = saved["reference"]["revenue_by_month"]
            referenced_at = saved["reference"]["computed_at"]
        else:
            session_id, runs = _drive(client, TURNS, args.turn_timeout)
    if not args.rescan:
        refs = reference(MONTHS)
        referenced_at = datetime.now(UTC).isoformat(timespec="seconds")
    transcript = [f"# Live smoke: {args.label} (code `{args.revision}`)", ""]
    for index, run in enumerate(runs):
        run.update(trace_facts(_spans(base, run["run_id"], 60.0)))
        month = MONTHS[index]
        expected = refs.get(month)
        run["expected_month"] = month
        run["expected_revenue"] = expected
        run["stated"] = expected is not None and number_in_text(
            expected, 0.01, run["answer"], share=False
        )
        transcript += [f"## Turn {index + 1}: `{run['run_id']}` ({run['status']})"]
        transcript += ["", f"**User:** {TURNS[index]}", ""]
        if run["question"]:
            transcript += [f"**Clarification asked:** {run['question']}", ""]
        for number, sql in enumerate(run["sql"], 1):
            transcript += [f"Query {number}:", "", "```sql", sql.strip(), "```", ""]
        transcript += ["**Released:**", "", run["answer"].strip() or "_nothing_", ""]
    result = {
        "label": args.label,
        "code_revision": args.revision,
        "started_at": started,
        "mode": health.get("mode"),
        "execution_backend": health.get("execution_backend"),
        "warehouse": "BigQuery bigquery-public-data.thelook_ecommerce (live)",
        "configured_models": {
            "google-interactions": settings.agent_gemini_model,
            "openai": settings.agent_openai_model,
        },
        "principal": "exec-local-admin (every product)",
        "session_id": session_id,
        "reference": {"computed_at": referenced_at, "revenue_by_month": refs},
        "runs": [
            {k: v for k, v in r.items() if k not in ("answer", "sql")} for r in runs
        ],
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    assert_no_sensitive(text)
    body = "\n".join(line.rstrip() for line in "\n".join(transcript).splitlines())
    assert_no_sensitive(body)
    (args.out / "transcripts").mkdir(parents=True, exist_ok=True)
    (args.out / f"{args.label}.json").write_text(text + "\n", encoding="utf-8")
    (args.out / "transcripts" / f"{args.label}.md").write_text(
        body.rstrip() + "\n", encoding="utf-8"
    )
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
