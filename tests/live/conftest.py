"""Live tests read credentials from the repository's ``.env``.

Without that file (a fresh clone or a task worktree) the configuration is
incomplete, and since the application defaults to live mode, loading it would
raise instead of reporting "not configured". Such tests are skipped, as the
``live`` marker promises; with a ``.env`` nothing changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if ENV_FILE.exists():
        return
    skip = pytest.mark.skip(reason="no .env: live credentials not configured")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)
