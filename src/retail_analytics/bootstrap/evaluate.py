"""Composition root for ``retail-analytics-eval``.

Commands (run from the repository root; also ``python -m
retail_analytics.bootstrap.evaluate``):

- ``run``: execute a manifest against a target, write the result JSON, print a
  summary. Exit 0 passed, 1 failed, 3 incomplete (blocked or nothing passed).
- ``compare``: compare a result with a baseline result.
- ``summary``: print the readable summary of a stored result.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import click

from retail_analytics.adapters.evaluation.files import (
    ReplayTarget,
    load_manifest,
    load_result,
    write_result,
)
from retail_analytics.application.evaluation.compare import compare_runs
from retail_analytics.application.evaluation.results import VERSION_KEYS
from retail_analytics.application.evaluation.runner import RunConfig, run_manifest
from retail_analytics.application.evaluation.summary import (
    render_comparison,
    render_summary,
)
from retail_analytics.application.ports.evaluation import EvaluationTarget
from retail_analytics.bootstrap.config import ConfigError, load_backend_settings

EXIT_FAILED = 1
EXIT_INCOMPLETE = 3


def detect_capabilities() -> frozenset[str]:
    """Names of live capabilities configured in the environment (never values)."""
    try:
        settings = load_backend_settings()
    except ConfigError:
        return frozenset()
    found = set()
    if settings.bigquery_project is not None:
        found.add("bigquery")
    if settings.gemini_api_key is not None:
        found.add("model_provider")
    return frozenset(found)


def _load_target(spec: str, observations: Path | None) -> EvaluationTarget:
    if spec == "replay":
        if observations is None:
            raise click.UsageError("--target replay needs --observations FILE")
        return ReplayTarget(observations)
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise click.UsageError("--target must be 'replay' or 'package.module:factory'")
    factory = getattr(importlib.import_module(module_name), attr)
    target: EvaluationTarget = factory()
    return target


def _parse_versions(pairs: tuple[str, ...]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or key not in VERSION_KEYS or not value:
            raise click.UsageError(
                f"--version expects KEY=VALUE with KEY in {', '.join(VERSION_KEYS)}"
            )
        versions[key] = value
    return versions


@click.group()
def main() -> None:
    """Repeatable evaluation runner."""


@main.command()
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, path_type=Path),
    required=True,
)
@click.option("--target", "target_spec", default="replay", show_default=True)
@click.option("--observations", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--mode",
    type=click.Choice(["fixture", "live"]),
    default="fixture",
    show_default=True,
)
@click.option("--out", "out_path", type=click.Path(path_type=Path), required=True)
@click.option("--baseline", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--version",
    "versions",
    multiple=True,
    help="KEY=VALUE component version to record.",
)
@click.option(
    "--capability",
    "capabilities",
    multiple=True,
    help="Declare an available capability.",
)
@click.option(
    "--detect-capabilities",
    "detect_capabilities_flag",
    is_flag=True,
    help="Add capabilities found in the environment.",
)
@click.option("--level", "levels", type=click.IntRange(1, 3), multiple=True)
@click.option("--tag", "tags", multiple=True)
@click.option(
    "--timestamp/--no-timestamp",
    default=False,
    help="Record recorded_at (off keeps files reproducible).",
)
def run(
    manifest_path: Path,
    target_spec: str,
    observations: Path | None,
    mode: str,
    out_path: Path,
    baseline: Path | None,
    versions: tuple[str, ...],
    capabilities: tuple[str, ...],
    detect_capabilities_flag: bool,
    levels: tuple[int, ...],
    tags: tuple[str, ...],
    timestamp: bool,
) -> None:
    """Run a manifest and write the result file."""
    available = set(capabilities)
    if detect_capabilities_flag:
        available |= detect_capabilities()
    config = RunConfig(
        mode="live" if mode == "live" else "fixture",
        available_capabilities=frozenset(available),
        versions=_parse_versions(versions),
        levels=frozenset(levels),
        tags=frozenset(tags),
    )
    result = run_manifest(
        load_manifest(manifest_path),
        _load_target(target_spec, observations),
        config,
        recorded_at=(lambda: datetime.now(UTC).isoformat(timespec="seconds"))
        if timestamp
        else None,
    )
    write_result(out_path, result)
    click.echo(render_summary(result))
    if baseline is not None:
        click.echo(render_comparison(compare_runs(load_result(baseline), result)))
    click.echo(f"result written to {out_path}")
    raise SystemExit(
        {"passed": 0, "failed": EXIT_FAILED, "incomplete": EXIT_INCOMPLETE}[
            result.verdict
        ]
    )


@main.command()
@click.argument("baseline", type=click.Path(exists=True, path_type=Path))
@click.argument("current", type=click.Path(exists=True, path_type=Path))
def compare(baseline: Path, current: Path) -> None:
    """Compare CURRENT with BASELINE result files."""
    click.echo(
        render_comparison(compare_runs(load_result(baseline), load_result(current)))
    )


@main.command()
@click.argument("result", type=click.Path(exists=True, path_type=Path))
def summary(result: Path) -> None:
    """Print the readable summary of a stored RESULT."""
    click.echo(render_summary(load_result(result)))


if __name__ == "__main__":
    main()
