"""The operator restore command re-validates withdrawn reuse links (T18-F5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.application.reports import ReportService
from retail_analytics.bootstrap import maintenance
from retail_analytics.bootstrap.config import load_backend_settings


def test_restore_command_wires_the_reuse_revalidation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = load_backend_settings(
        environ={
            # Never connected: composition only.
            "APP_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:1/x",
            "ARTIFACT_DIR": str(tmp_path),
        },
        env_file=None,
    )
    monkeypatch.setattr(maintenance, "load_backend_settings", lambda: settings)
    persistence, service = maintenance._open()
    try:
        assert isinstance(service._reuse, ReportService)
    finally:
        persistence.close()
