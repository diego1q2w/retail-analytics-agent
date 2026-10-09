"""Operator commands: ``retail-analytics-maintenance``.

``cleanup`` is the scheduled lifecycle job (run it from cron or any scheduler;
it is idempotent and bounded, so overlapping or repeated runs are safe).
``restore`` is the manual recovery of a soft-deleted report; there is no agent
tool for it. Every command prints identifiers, dates and counts only, never
report titles or content, and runs with the backend's own settings against its
database, so it is for trusted operators.

    retail-analytics-maintenance cleanup --dry-run
    retail-analytics-maintenance cleanup
    retail-analytics-maintenance list-restorable --as <executive-id>
    retail-analytics-maintenance restore <report-id> --as <executive-id>
    retail-analytics-maintenance unresolved
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import click

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.lifecycle import (
    MaintenanceLimits,
    MaintenanceReport,
)
from retail_analytics.application.lifecycle import LifecycleService
from retail_analytics.bootstrap.access import build_access
from retail_analytics.bootstrap.artifacts import build_artifacts
from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    load_backend_settings,
)
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.lifecycle import build_lifecycle
from retail_analytics.bootstrap.persistence import (
    Persistence,
    persistence_from_settings,
)
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.bootstrap.reports import build_reports
from retail_analytics.domain.access import Permission
from retail_analytics.domain.lifecycle import RestoreError

_ALL_SCOPES = frozenset(p.value for p in Permission)


def _open() -> tuple[Persistence, LifecycleService]:
    try:
        settings: BackendSettings = load_backend_settings()
        persistence = persistence_from_settings(settings)
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    access = build_access(persistence, verifier=None)  # type: ignore[arg-type]
    artifacts = build_artifacts(settings, persistence)
    evidence = build_evidence(persistence, settings=settings)
    context = build_context(
        persistence, access, evidence, build_preferences(persistence, access)
    )
    # Re-validates a restored report's withdrawn reuse links (T18-F5).
    reports = build_reports(
        persistence, artifacts.service, evidence, context.gate, access.resolver
    )
    service = build_lifecycle(
        persistence,
        access.resolver,
        artifacts,
        settings=settings,
        reuse=reports,
    )
    return persistence, service


def format_report(report: MaintenanceReport) -> list[str]:
    """One ``name=count`` line per figure."""
    lines = [f"mode={'dry-run' if report.dry_run else 'run'}"]
    for name, value in asdict(report).items():
        if name == "dry_run":
            continue
        if isinstance(value, dict):
            lines.extend(f"{name}.{key}={count}" for key, count in value.items())
        else:
            lines.append(
                f"{name}={str(value).lower() if isinstance(value, bool) else value}"
            )
    return lines


@click.group()
def main() -> None:
    """Report recovery and lifecycle cleanup (trusted operators only)."""


@main.command()
@click.option("--dry-run", is_flag=True, help="Report counts only; change nothing.")
@click.option("--max-reports", type=click.IntRange(1, 10000), default=None)
@click.option("--max-sessions", type=click.IntRange(1, 10000), default=None)
@click.option("--max-audit-rows", type=click.IntRange(1, 100000), default=1000)
def cleanup(
    dry_run: bool,
    max_reports: int | None,
    max_sessions: int | None,
    max_audit_rows: int,
) -> None:
    """Purge reports past recovery, expire investigations, trim audits."""
    persistence, service = _open()
    try:
        defaults = MaintenanceLimits()
        report = asyncio.run(
            service.run_maintenance(
                MaintenanceLimits(
                    max_reports=max_reports or defaults.max_reports,
                    max_sessions=max_sessions or defaults.max_sessions,
                    max_audit_rows=max_audit_rows,
                ),
                dry_run=dry_run,
            )
        )
    finally:
        persistence.close()
    for line in format_report(report):
        click.echo(line)


@main.command("list-restorable")
@click.option("--as", "actor", required=True, help="Executive ID of the owner.")
def list_restorable(actor: str) -> None:
    """List the owner's deleted reports that can still be restored."""
    persistence, service = _open()
    try:
        found = asyncio.run(service.list_restorable(Principal(actor, _ALL_SCOPES)))
    except AccessDenied:
        raise click.ClickException("not permitted") from None
    finally:
        persistence.close()
    for item in found:
        click.echo(
            f"{item.report_id} deleted_at={item.deleted_at.isoformat()} "
            f"recoverable_until={item.recoverable_until.isoformat()}"
        )
    if not found:
        click.echo("no restorable reports")


@main.command()
@click.argument("report_id")
@click.option(
    "--as",
    "actor",
    required=True,
    help="Executive ID acting: the report's owner, or an access administrator.",
)
def restore(report_id: str, actor: str) -> None:
    """Restore a soft-deleted report before its recovery deadline."""
    persistence, service = _open()
    try:
        restored = asyncio.run(
            service.restore(Principal(actor, _ALL_SCOPES), report_id)
        )
    except (AccessDenied, RestoreError) as exc:
        message = exc.message if isinstance(exc, RestoreError) else "no such report"
        raise click.ClickException(message) from None
    finally:
        persistence.close()
    click.echo(f"restored {restored.report_id}")
    if restored.reuse_links_pending:
        reuse = restored.reuse
        reinstated = 0 if reuse is None else reuse.reinstated
        refused = restored.reuse_links_pending - reinstated
        click.echo(
            f"reuse_links_revalidated={restored.reuse_links_pending} "
            f"reinstated={reinstated} still_withdrawn={refused}"
        )


@main.command()
@click.option("--limit", type=click.IntRange(1, 1000), default=100)
def unresolved(limit: int) -> None:
    """List operations flagged for manual resolution (kept from cleanup)."""
    persistence, service = _open()
    try:
        found = asyncio.run(service.unresolved_operations(limit))
    finally:
        persistence.close()
    for op in found:
        click.echo(
            f"{op.operation_id} run={op.run_id} capability={op.capability} "
            f"status={op.status} since={op.since.isoformat()}"
        )
    if not found:
        click.echo("no flagged operations")


if __name__ == "__main__":
    main()
