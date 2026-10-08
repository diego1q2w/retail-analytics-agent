"""Executable layer boundaries for the real package, plus proof they can fail."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.boundaries import (
    INNER_LAYERS,
    LAYERS,
    check_import_time,
    check_sources,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
PACKAGE = "retail_analytics"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "invalid_tree"
FIXTURE_PACKAGE = "fixture_app"


def test_every_layer_package_exists() -> None:
    for layer in LAYERS:
        assert (SRC / PACKAGE / layer / "__init__.py").is_file(), layer


def test_source_imports_respect_layer_rules() -> None:
    violations = check_sources(SRC / PACKAGE, PACKAGE)
    assert violations == [], "\n".join(map(str, violations))


@pytest.mark.parametrize("layer", INNER_LAYERS)
def test_inner_layers_import_without_sdks_or_side_effects(layer: str) -> None:
    problems = check_import_time(SRC, PACKAGE, layer)
    assert problems == [], "\n".join(problems)


def test_source_check_rejects_invalid_fixture() -> None:
    found = {
        (v.module, v.detail)
        for v in check_sources(FIXTURE_ROOT / FIXTURE_PACKAGE, FIXTURE_PACKAGE)
    }
    expected = {
        (
            "fixture_app.domain.reverse",
            "domain must not import adapters (fixture_app.adapters)",
        ),
        ("fixture_app.domain.sdk", "domain must not import httpx"),
        ("fixture_app.domain.hidden", "domain must not import google"),
        ("fixture_app.application.dynamic", "application must not use importlib"),
        ("fixture_app.application.framework", "application must not import sqlglot"),
        (
            "fixture_app.interfaces.wiring",
            "interfaces must not import adapters (fixture_app.adapters.store)",
        ),
    }
    assert expected <= found
    assert not any(module.startswith("fixture_app.bootstrap") for module, _ in found)


def test_import_time_check_rejects_invalid_fixture() -> None:
    domain = check_import_time(FIXTURE_ROOT, FIXTURE_PACKAGE, "domain")
    assert "domain import loads httpx" in domain
    assert "domain import side effect: environ read FIXTURE_PROJECT" in domain
    assert "domain import side effect: open null" in domain

    application = check_import_time(FIXTURE_ROOT, FIXTURE_PACKAGE, "application")
    assert "application import loads sqlglot" in application
    # Effects pydantic performs for itself are exempt; the module's own are not.
    assert (
        "application import side effect: environ read FIXTURE_APP_PROJECT"
        in application
    )
    assert not any("PYDANTIC" in problem for problem in application)
