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
    "answered_model",
    "status",
    "decision",
    "topic",
    "classifier_version",
    "cost_status",
    "cost_usd",
    "model_cost_usd",
    "model_cost_complete",
    "error.type",
)
_MAX_VALUE = 80


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


def _detail(span: Mapping[str, object]) -> str:
    attributes = span_attributes(span)
    shown = {key: attributes[key] for key in _SHOWN if key in attributes}
    return " ".join(f"{key}={_clip(value)}" for key, value in shown.items())


def _clip(value: str) -> str:
    # One short token per value; the allowlist above already excludes payloads.
    text = "".join(c if c.isprintable() else " " for c in value).strip()
    return text if len(text) <= _MAX_VALUE else text[: _MAX_VALUE - 1] + "~"


def _start(span: Mapping[str, object]) -> int:
    try:
        return int(str(span.get("start_time_unix_nano", 0)))
    except ValueError:
        return 0


def span_lines(spans: Sequence[Mapping[str, object]]) -> list[str]:
    """An indented span tree, parents before children.

    Recorded traces can be incomplete (the root is written when the run
    closes) or, for traces recorded before the parentage fix, malformed (a
    parent cycle with no root). Every span is still listed once with its
    details: spans whose parent is missing start their own branch, spans in a
    cycle are listed after a warning, and nothing recurses.
    """
    by_id: dict[str, Mapping[str, object]] = {}
    for span in spans:
        span_id = span.get("span_id")
        if isinstance(span_id, str) and span_id:
            by_id.setdefault(span_id, span)

    def parent_of(span: Mapping[str, object]) -> str | None:
        parent = span.get("parent_span_id")
        return parent if isinstance(parent, str) and parent else None

    children: dict[str, list[Mapping[str, object]]] = {}
    roots: list[Mapping[str, object]] = []
    orphans: list[Mapping[str, object]] = []
    for span in spans:
        parent = parent_of(span)
        if parent is None:
            roots.append(span)
        elif parent in by_id:
            children.setdefault(parent, []).append(span)
        else:
            orphans.append(span)

    lines: list[str] = []
    seen: set[int] = set()

    def walk(top: Mapping[str, object], marker: str) -> None:
        stack: list[tuple[Mapping[str, object], int, str]] = [(top, 0, marker)]
        while stack:
            span, depth, mark = stack.pop()
            if id(span) in seen:
                continue
            seen.add(id(span))
            detail = " ".join(part for part in (mark, _detail(span)) if part)
            lines.append(f"{'  ' * depth}{span.get('name')}  {detail}".rstrip())
            span_id = span.get("span_id")
            kids = children.get(span_id, []) if isinstance(span_id, str) else []
            for kid in sorted(kids, key=_start, reverse=True):
                if id(kid) not in seen:
                    stack.append((kid, depth + 1, ""))

    for span in sorted(roots, key=_start):
        walk(span, "")
    for span in sorted(orphans, key=_start):
        walk(span, "[parent missing]")
    cyclic = [span for span in spans if id(span) not in seen]
    for span in sorted(cyclic, key=_start):
        walk(span, "[in parent cycle]")

    notes: list[str] = []
    if cyclic:
        notes.append(
            f"warning: malformed span hierarchy: {len(cyclic)} span(s) form a "
            "parent cycle; listed flat below the other spans."
        )
    if orphans:
        notes.append(
            f"note: {len(orphans)} span(s) have a parent missing from the trace "
            "(the run may still be open, or export dropped spans)."
        )
    if not roots and spans:
        notes.append("note: the trace has no root span.")
    elif len(roots) > 1:
        notes.append(f"warning: the trace has {len(roots)} root spans.")
    return notes + lines


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
