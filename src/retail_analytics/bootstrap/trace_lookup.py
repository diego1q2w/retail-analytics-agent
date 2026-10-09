"""Find the MLflow trace of a run (``python -m ...bootstrap.trace_lookup``).

Every component derives the trace id from the run id, so the id printed in a
progress event or API response is enough. ``--tree`` fetches the trace from
MLflow and prints its spans (name, outcome, error code, provider) without any
payload, since the trace holds none.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import click
import httpx

from retail_analytics.application.telemetry import trace_id_for
from retail_analytics.bootstrap.entrypoint import settings_or_exit

_SHOWN = (
    "capability",
    "operation_id",
    "attempt",
    "outcome",
    "error_code",
    "reason",
    "job_id",
    "provider",
    "model",
    "fallback_from",
    "fallback_reason",
    "answered_by",
    "status",
)


def mlflow_base(traces_endpoint: str) -> str:
    return traces_endpoint.removesuffix("/v1/traces").rstrip("/")


def trace_ref(run_id: str) -> str:
    return "tr-" + trace_id_for(run_id)


def _attribute(value: object) -> str:
    # MLflow stores attribute values JSON encoded.
    if isinstance(value, str):
        try:
            return str(json.loads(value))
        except ValueError:
            return value
    return str(value)


def span_attributes(span: Mapping[str, object]) -> dict[str, str]:
    """A span's attributes as text, from either MLflow response shape."""
    raw = span.get("attributes")
    found: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("value"), dict):
                values = list(item["value"].values())
                if values:
                    found[str(item.get("key"))] = str(values[0])
    elif isinstance(raw, dict):
        found = {str(key): _attribute(value) for key, value in raw.items()}
    return found


def span_lines(spans: Sequence[Mapping[str, object]]) -> list[str]:
    """An indented span tree, parents before children."""
    children: dict[str | None, list[Mapping[str, object]]] = {}
    for span in spans:
        parent = span.get("parent_span_id")
        children.setdefault(parent if isinstance(parent, str) else None, []).append(
            span
        )
    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        ordered = sorted(
            children.get(parent, []),
            key=lambda s: int(str(s.get("start_time_unix_nano", 0))),
        )
        for span in ordered:
            attributes = span_attributes(span)
            shown = {key: attributes[key] for key in _SHOWN if key in attributes}
            detail = " ".join(f"{key}={value}" for key, value in shown.items())
            lines.append(f"{'  ' * depth}{span.get('name')}  {detail}".rstrip())
            span_id = span.get("span_id")
            if isinstance(span_id, str):
                walk(span_id, depth + 1)

    walk(None, 0)
    if not lines:  # spans whose parent was never exported (e.g. root still open)
        lines = [f"{span.get('name')}" for span in spans]
    return lines


@click.command()
@click.argument("run_id")
@click.option("--tree", is_flag=True, help="Fetch the trace from MLflow and print it.")
def main(run_id: str, tree: bool) -> None:
    """Print the trace id and MLflow links of RUN_ID."""
    settings = settings_or_exit(False)
    base = mlflow_base(settings.telemetry_traces_endpoint)
    ref = trace_ref(run_id)
    click.echo(f"trace id:  {ref}")
    click.echo(
        f"MLflow UI: {base}/#/experiments/{settings.telemetry_experiment_id}/traces"
    )
    click.echo(f"MLflow API: {base}/api/3.0/mlflow/traces/batchGet?trace_ids={ref}")
    if not tree:
        return
    try:
        response = httpx.get(
            f"{base}/api/3.0/mlflow/traces/batchGet",
            params={"trace_ids": ref},
            timeout=10,
        )
        response.raise_for_status()
        traces = response.json().get("traces", [])
    except (httpx.HTTPError, ValueError):
        raise SystemExit("MLflow is not reachable or has no such trace.") from None
    if not traces:
        raise SystemExit("MLflow has no spans for this run yet.")
    for line in span_lines(traces[0].get("spans", [])):
        click.echo(line)


if __name__ == "__main__":
    main()
