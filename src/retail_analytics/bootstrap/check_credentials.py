"""Composition root for ``retail-analytics-check-credentials``.

Verifies Google access: application default credentials, metadata for the four
public tables, a cost-bounded dry run and a minimal Gemini request. Output holds
no secrets; exit status is 0 when every check passes, 1 otherwise, 2 for
invalid configuration.
"""

from __future__ import annotations

import click

from retail_analytics.adapters.google_access import (
    BigQueryWarehouseAccess,
    GeminiModelAccess,
)
from retail_analytics.application.access_check import (
    CheckResult,
    ModelAccess,
    WarehouseAccess,
    check_model,
    check_warehouse,
)
from retail_analytics.bootstrap.config import (
    BACKEND_ENV_PREFIX,
    BackendSettings,
    ConfigError,
    load_backend_settings,
)
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE


def build_warehouse(settings: BackendSettings) -> WarehouseAccess | None:
    if settings.bigquery_project is None:
        return None
    return BigQueryWarehouseAccess(
        settings.bigquery_project, settings.bigquery_location
    )


def build_model(settings: BackendSettings) -> ModelAccess | None:
    if settings.gemini_api_key is None:
        return None
    return GeminiModelAccess(
        settings.gemini_api_key.get_secret_value(), settings.gemini_model
    )


def _missing(name: str, setting: str) -> CheckResult:
    return CheckResult(
        name,
        False,
        f"{BACKEND_ENV_PREFIX}{setting} is not set",
        f"set {BACKEND_ENV_PREFIX}{setting} in your ignored .env "
        "(see docs/google-access.md)",
    )


def run_checks(settings: BackendSettings) -> list[CheckResult]:
    warehouse = build_warehouse(settings)
    model = build_model(settings)
    results = (
        check_warehouse(warehouse)
        if warehouse
        else [_missing("bigquery project", "BIGQUERY_PROJECT")]
    )
    results.append(
        check_model(model) if model else _missing("gemini request", "GEMINI_API_KEY")
    )
    return results


@click.command()
def main() -> None:
    """Check BigQuery and Gemini access without printing any secret."""
    try:
        settings = load_backend_settings()
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    results = run_checks(settings)
    for result in results:
        click.echo(f"[{'ok' if result.ok else 'FAIL'}] {result.name}: {result.detail}")
        if result.remedy:
            click.echo(f"       fix: {result.remedy}")
    failed = sum(not r.ok for r in results)
    click.echo(f"{len(results) - failed}/{len(results)} checks passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
