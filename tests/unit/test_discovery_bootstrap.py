"""Composition of discovery from settings."""

from __future__ import annotations

import pytest

from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
from retail_analytics.bootstrap.discovery import build_discovery
from tests.unit.discovery_fixtures import FakeClock, StubMetadata, context


def test_live_discovery_requires_a_bigquery_project() -> None:
    with pytest.raises(ConfigError):
        build_discovery(
            load_backend_settings(environ={"APP_MODE": "fixture"}, env_file=None)
        )


def test_refresh_interval_setting_is_bounded() -> None:
    settings = load_backend_settings(
        environ={"APP_MODE": "fixture", "SCHEMA_REFRESH_SECONDS": "600"}, env_file=None
    )
    assert settings.schema_refresh_seconds == 600
    with pytest.raises(ConfigError):
        load_backend_settings(
            environ={"APP_MODE": "fixture", "SCHEMA_REFRESH_SECONDS": "5"},
            env_file=None,
        )


@pytest.mark.asyncio
async def test_build_with_stub_provider_serves_the_catalog() -> None:
    service = build_discovery(
        load_backend_settings(environ={"APP_MODE": "fixture"}, env_file=None),
        provider=StubMetadata(),
        clock=FakeClock(),
    )
    assert (await service.list_relations(context())).catalog.relations
