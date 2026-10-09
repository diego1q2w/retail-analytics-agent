"""Executable layer boundaries for the real package, plus proof they can fail."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture import boundaries
from tests.architecture.boundaries import (
    EXCEPTIONS,
    INNER_LAYERS,
    LAYERS,
    check_application_layout,
    check_import_time,
    check_sources,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
PACKAGE = "retail_analytics"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "invalid_tree"
FIXTURE_PACKAGE = "fixture_app"
LAYOUT_FIXTURE = Path(__file__).parent / "fixtures" / "invalid_layout" / FIXTURE_PACKAGE


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


def test_every_exception_has_a_reason() -> None:
    for key, reason in EXCEPTIONS.items():
        assert reason.strip(), f"exception {key} needs a reason"


def test_application_layout_matches_convention() -> None:
    violations = check_application_layout(SRC / PACKAGE, PACKAGE)
    assert violations == [], "\n".join(map(str, violations))


def _layout_details() -> set[tuple[str, str]]:
    return {
        (v.module, v.detail)
        for v in check_application_layout(LAYOUT_FIXTURE, FIXTURE_PACKAGE)
    }


APP = "fixture_app.application"


@pytest.mark.parametrize(
    ("module", "detail"),
    [
        # Rule 1: Protocols only in application/ports.
        (f"{APP}.orders", "Protocol LocalPort must live in application/ports"),
        (
            f"{APP}.contracts.bad",
            "Protocol RecordSource must live in application/ports",
        ),
        # Rule 2: ports contain only Protocols.
        (f"{APP}.ports.bad", "ports may only define Protocols (OrderRecord)"),
        (f"{APP}.ports.bad", "ports may only define Protocols (ConcreteStore)"),
        (f"{APP}.ports.bad", "ports hold no values or data (assignment)"),
        (
            f"{APP}.ports.bad",
            "ports may only contain Protocols, imports and type aliases (FunctionDef)",
        ),
        (
            f"{APP}.ports.bad",
            "port methods must be bodiless (docstring, ... or pass) (LogicPort.fetch)",
        ),
        # Rule 3: contracts hold no services and keep the wire-contract base.
        (
            f"{APP}.contracts.bad",
            "contract OrderService looks like a service: service-like class name",
        ),
        (
            f"{APP}.contracts.bad",
            "contract Holder looks like a service: "
            "hand-written __init__ (injected collaborators)",
        ),
        (f"{APP}.contracts", "contracts/__init__ must define ContractModel"),
        # Rule 4: ports/contracts never import services, adapters, interfaces,
        # bootstrap; adapters import ports from application.ports.
        (
            f"{APP}.ports.bad",
            f"ports must not import the service module {APP}.orders",
        ),
        (
            f"{APP}.ports.bad",
            "ports must not import adapters (fixture_app.adapters.impl)",
        ),
        (
            f"{APP}.contracts.bad",
            f"contracts must not import the service module {APP}.orders",
        ),
        (
            f"{APP}.contracts.bad",
            "contracts must not import adapters (fixture_app.adapters.impl)",
        ),
        (
            f"{APP}.contracts.bad",
            "contracts must not import interfaces (fixture_app.interfaces.web)",
        ),
        (
            f"{APP}.contracts.bad",
            "contracts must not import bootstrap (fixture_app.bootstrap.main)",
        ),
        (f"{APP}.contracts.bad", "contracts must not import ports"),
        (
            "fixture_app.adapters.impl",
            "adapters must import port GoodPort from application.ports, "
            f"not {APP}.orders",
        ),
    ],
)
def test_layout_check_rejects_invalid_fixture(module: str, detail: str) -> None:
    assert (module, detail) in _layout_details()


def test_layout_check_accepts_valid_port_module() -> None:
    assert not any(module == f"{APP}.ports.good" for module, _ in _layout_details())


def test_layout_exceptions_are_honoured_by_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _layout_details()
    reasons = {
        (f"{APP}.orders", "LocalPort"): "fixture: proves exceptions are keyed",
        (
            f"{APP}.contracts.bad",
            f"{APP}.orders",
        ): "fixture: proves import exceptions are keyed",
    }
    monkeypatch.setattr(boundaries, "EXCEPTIONS", {**EXCEPTIONS, **reasons})
    after = _layout_details()
    assert before - after == {
        (f"{APP}.orders", "Protocol LocalPort must live in application/ports"),
        (
            f"{APP}.contracts.bad",
            f"contracts must not import the service module {APP}.orders",
        ),
    }
