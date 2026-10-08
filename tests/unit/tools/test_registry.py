"""Registry safety rules and the permission-filtered catalog."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ConfigDict

from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilityRegistry,
    CapabilitySpec,
    RegistrationError,
    RetrySpec,
    ToolInput,
)
from retail_analytics.application.tools.registry import (
    RESERVED_ARGUMENT_NAMES,
    RESERVED_ARGUMENT_TOKENS,
)
from retail_analytics.domain.operations import RecoveryMode, SideEffect
from tests.unit.tools.fakes import (
    CHART,
    DELIVER,
    SQL,
    Calls,
    SqlInput,
    chart_spec,
    delivery_spec,
    execution_context,
    sql_spec,
)


def _with_input(model: Any) -> CapabilitySpec[Any, Any]:
    spec: CapabilitySpec[Any, Any] = sql_spec(Calls())
    return replace(spec, input_model=model)


def _registry() -> CapabilityRegistry:
    calls = Calls()
    return CapabilityRegistry(
        [sql_spec(calls), chart_spec(calls), delivery_spec(calls)]
    )


TRUSTED_FIELD_NAMES = (
    "executive_id",
    "entitlements",
    "entitlement_version",
    "product_scope",
    "budget",
    "remaining_budget_bytes",
    "confirmed",
    "confirmation_token",
    "approved",
    "permissions",
    "session_id",
    "run_id",
)


def test_model_schemas_omit_trusted_context() -> None:
    catalog = _registry().catalog(execution_context())
    assert {tool.name for tool in catalog} == {SQL, CHART, DELIVER}
    text = json.dumps([tool.parameters for tool in catalog]).lower()
    for name in TRUSTED_FIELD_NAMES:
        assert f'"{name}"' not in text
    for token in RESERVED_ARGUMENT_TOKENS:
        assert token not in text


@pytest.mark.parametrize("field_name", TRUSTED_FIELD_NAMES)
def test_input_declaring_trusted_field_is_rejected(field_name: str) -> None:
    model = type(
        "Sneaky",
        (ToolInput,),
        {"__annotations__": {field_name: str}, "__module__": __name__},
    )
    with pytest.raises(RegistrationError, match="trusted context"):
        _with_input(model)


def test_nested_trusted_field_is_rejected() -> None:
    class Scope(ToolInput):
        entitlement_version: int

    class Outer(ToolInput):
        sql: str
        scope: Scope

    with pytest.raises(RegistrationError, match="entitlement_version"):
        _with_input(Outer)


def test_reserved_names_cover_correlation_ids() -> None:
    assert {"session_id", "run_id", "operation_id"} <= RESERVED_ARGUMENT_NAMES


def test_permissive_input_model_is_rejected() -> None:
    class Loose(SqlInput):
        model_config = ConfigDict(extra="allow")

    with pytest.raises(RegistrationError, match="forbid extra"):
        _with_input(Loose)


def test_non_contract_model_is_rejected() -> None:
    with pytest.raises(RegistrationError, match="must subclass ToolInput"):
        _with_input(dict)


@pytest.mark.parametrize(
    "effect", [SideEffect.EXTERNAL_JOB, SideEffect.EXTERNAL_DELIVERY]
)
def test_external_effects_cannot_retry_blindly(effect: SideEffect) -> None:
    with pytest.raises(RegistrationError, match="reconcile"):
        replace(
            sql_spec(Calls()),
            side_effect=effect,
            retry=RetrySpec(RecoveryMode.RETRY, 3, timedelta(seconds=5)),
        )


def test_retry_spec_is_bounded_and_consistent() -> None:
    with pytest.raises(RegistrationError):
        RetrySpec(RecoveryMode.NO_RETRY, 2, timedelta(seconds=5))
    with pytest.raises(RegistrationError):
        RetrySpec(RecoveryMode.RETRY, 0, timedelta(seconds=5))
    with pytest.raises(RegistrationError):
        RetrySpec(RecoveryMode.RETRY, 1, timedelta(0))


@pytest.mark.parametrize("name", ["", "Execute", "drop table", "x" * 65])
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(RegistrationError):
        replace(sql_spec(Calls()), name=name)


def test_duplicate_names_are_rejected() -> None:
    calls = Calls()
    with pytest.raises(RegistrationError, match="duplicate"):
        CapabilityRegistry([sql_spec(calls), sql_spec(calls)])


def test_catalog_is_permission_filtered_and_stable() -> None:
    registry = _registry()
    full = execution_context()
    assert registry.catalog(full) == registry.catalog(full)
    assert [tool.name for tool in registry.catalog(full)] == sorted(
        [SQL, CHART, DELIVER]
    )

    no_delivery = execution_context(permissions=frozenset({"analyze"}))
    assert {t.name for t in registry.catalog(no_delivery)} == {SQL, CHART}

    # Empty entitlements mean no data, so data-scoped tools disappear.
    no_products = execution_context(products=frozenset())
    assert {t.name for t in registry.catalog(no_products)} == {CHART, DELIVER}

    assert registry.catalog(execution_context(permissions=frozenset())) == ()


def test_resolve_hides_unknown_and_unauthorized_alike() -> None:
    registry = _registry()
    limited = execution_context(permissions=frozenset({"analyze"}))
    assert registry.resolve(DELIVER, limited) is None
    assert registry.resolve("no_such_tool", limited) is None
    resolved = registry.resolve(SQL, limited)
    assert resolved is not None
    assert resolved.authorization == AuthorizationSpec(
        required_permissions=frozenset({"analyze"}), requires_product_scope=True
    )
