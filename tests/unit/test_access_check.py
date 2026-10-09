"""Access-check use case and Google adapter error mapping, with fakes only."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner
from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions
from google.genai import errors as genai_errors

from retail_analytics.adapters.google_access import (
    BigQueryWarehouseAccess,
    GeminiModelAccess,
)
from retail_analytics.application.access_check import (
    DRY_RUN_BYTES_LIMIT,
    REQUIRED_TABLES,
    AccessError,
    check_model,
    check_warehouse,
)
from retail_analytics.application.contracts.access_check import TableMetadata
from retail_analytics.bootstrap import check_credentials

SECRET = "AIza-super-secret"


class FakeWarehouse:
    def __init__(
        self,
        creds: AccessError | None = None,
        broken_table: str | None = None,
        scanned: int = 1000,
    ) -> None:
        self.creds = creds
        self.broken_table = broken_table
        self.scanned = scanned
        self.tables: list[str] = []

    def credentials_ready(self) -> None:
        if self.creds:
            raise self.creds

    def table_metadata(self, table: str) -> TableMetadata:
        self.tables.append(table)
        if self.broken_table and table.endswith(self.broken_table):
            raise AccessError("permission denied (HTTP 403)", "enable the API")
        return TableMetadata(table, 10, 3)

    def dry_run_bytes(self, sql: str) -> int:
        return self.scanned


class FakeModel:
    model_name = "fake-model"

    def __init__(self, error: AccessError | None = None) -> None:
        self.error = error

    def ping(self) -> None:
        if self.error:
            raise self.error


def test_warehouse_all_checks_pass() -> None:
    fake = FakeWarehouse()
    results = check_warehouse(fake)
    assert all(r.ok for r in results)
    assert len(fake.tables) == len(REQUIRED_TABLES) == 4
    assert [r.name for r in results][-1] == "bigquery dry run"


def test_missing_credentials_stops_after_one_actionable_result() -> None:
    fake = FakeWarehouse(
        creds=AccessError("no application default credentials", "login")
    )
    results = check_warehouse(fake)
    assert len(results) == 1
    assert not results[0].ok
    assert results[0].remedy == "login"
    assert fake.tables == []


def test_one_broken_table_is_reported_and_others_still_checked() -> None:
    results = check_warehouse(FakeWarehouse(broken_table="products"))
    failed = [r for r in results if not r.ok]
    assert [r.name for r in failed] == ["bigquery table products"]
    assert len(results) == 6


def test_dry_run_over_limit_fails() -> None:
    results = check_warehouse(FakeWarehouse(scanned=DRY_RUN_BYTES_LIMIT + 1))
    assert not results[-1].ok


def test_model_check_success_and_failure() -> None:
    assert check_model(FakeModel()).ok
    bad = check_model(FakeModel(AccessError("rejected", "new key")))
    assert not bad.ok
    assert bad.remedy == "new key"


class RaisingBigQuery:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_table(self, table: str) -> Any:
        raise self.error

    def query(self, sql: str, job_config: Any) -> Any:
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (auth_exceptions.RefreshError("token " + SECRET), "expired"),  # type: ignore[no-untyped-call]
        (api_exceptions.Forbidden("denied " + SECRET), "403"),  # type: ignore[no-untyped-call]
        (api_exceptions.NotFound("gone"), "not found"),  # type: ignore[no-untyped-call]
        (api_exceptions.Unauthorized("no"), "401"),  # type: ignore[no-untyped-call]
        (RuntimeError(SECRET), "RuntimeError"),
    ],
)
def test_bigquery_errors_are_mapped_without_leaking(
    error: Exception, expected: str
) -> None:
    access = BigQueryWarehouseAccess("proj", "US", RaisingBigQuery(error))  # type: ignore[arg-type]
    for call in (
        lambda: access.table_metadata("a.b.c"),
        lambda: access.dry_run_bytes("SELECT 1"),
    ):
        with pytest.raises(AccessError) as caught:
            call()
        assert expected in caught.value.problem
        assert SECRET not in caught.value.problem + caught.value.remedy


def test_missing_adc_gives_login_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_credentials(**_: Any) -> Any:
        raise auth_exceptions.DefaultCredentialsError("details " + SECRET)  # type: ignore[no-untyped-call]

    monkeypatch.setattr(
        "retail_analytics.adapters.google_access.default_credentials", no_credentials
    )
    with pytest.raises(AccessError) as caught:
        BigQueryWarehouseAccess("proj", "US").credentials_ready()
    assert "gcloud auth application-default login" in caught.value.remedy
    assert SECRET not in caught.value.problem


class FakeGenai:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.models = self

    def generate_content(self, **_: Any) -> Any:
        if self.error:
            raise self.error
        return object()


@pytest.mark.parametrize(
    ("code", "expected"),
    [(403, "rejected"), (400, "rejected"), (404, "not available"), (429, "rate limit")],
)
def test_gemini_http_errors_are_mapped(code: int, expected: str) -> None:
    error = genai_errors.APIError(code, {"error": {"message": SECRET}})
    access = GeminiModelAccess(SECRET, "m", FakeGenai(error))  # type: ignore[arg-type]
    with pytest.raises(AccessError) as caught:
        access.ping()
    assert expected in caught.value.problem
    assert SECRET not in caught.value.problem + caught.value.remedy


def test_gemini_success_and_unexpected_error() -> None:
    GeminiModelAccess(SECRET, "m", FakeGenai(None)).ping()  # type: ignore[arg-type]
    with pytest.raises(AccessError, match="OSError"):
        GeminiModelAccess(SECRET, "m", FakeGenai(OSError(SECRET))).ping()  # type: ignore[arg-type]


def test_command_reports_missing_settings_without_network() -> None:
    result = CliRunner().invoke(check_credentials.main, [])
    assert result.exit_code == 1
    assert "BIGQUERY_PROJECT is not set" in result.output
    assert "GEMINI_API_KEY is not set" in result.output
    assert "0/2 checks passed" in result.output


def test_command_success_never_prints_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_credentials, "build_warehouse", lambda s: FakeWarehouse())
    monkeypatch.setattr(check_credentials, "build_model", lambda s: FakeModel())
    result = CliRunner().invoke(
        check_credentials.main,
        [],
        env={
            "BIGQUERY_PROJECT": "proj",
            "GEMINI_API_KEY": SECRET,
        },
    )
    assert result.exit_code == 0, result.output
    assert "7/7 checks passed" in result.output
    assert SECRET not in result.output


def test_command_config_error_exits_2() -> None:
    result = CliRunner().invoke(check_credentials.main, [], env={"APP_MODE": "bogus"})
    assert result.exit_code == 2
