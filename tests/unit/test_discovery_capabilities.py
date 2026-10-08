"""The discovery tools through the shared gateway and registry."""

from __future__ import annotations

import pytest

from retail_analytics.application.tools import (
    CapabilityRegistry,
    OperationContext,
    ToolCall,
    ToolFailed,
    ToolSucceeded,
    invoke,
)
from retail_analytics.capabilities.discovery import (
    DESCRIBE_RELATION,
    LIST_RELATIONS,
    DescribeRelationOutput,
    ListRelationsOutput,
    discovery_capabilities,
)
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.discovery_fixtures import StubMetadata, context
from tests.unit.test_discovery import build
from tests.unit.tools.fakes import RecordingSink

pytestmark = pytest.mark.asyncio


def harness(
    stub: StubMetadata | None = None,
) -> tuple[CapabilityRegistry, StubMetadata]:
    service, _, stub, _ = build(stub)
    return CapabilityRegistry(discovery_capabilities(service)), stub


async def call(registry: CapabilityRegistry, tool: str, ctx, **args):  # type: ignore[no-untyped-def]
    return await invoke(
        registry,
        ToolCall(call_id="c-1", name=tool, arguments=args),
        OperationContext(execution=ctx, operation_id="op-1"),
        RecordingSink(),
    )


async def test_tools_are_registered_and_hidden_without_scope() -> None:
    registry, _ = harness()
    assert [t.name for t in registry.catalog(context())] == [
        DESCRIBE_RELATION,
        LIST_RELATIONS,
    ]
    assert not registry.catalog(context(products=frozenset()))
    result = await call(registry, LIST_RELATIONS, context(products=frozenset()))
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.ACCESS_DENIED


async def test_list_then_describe_returns_typed_fields() -> None:
    registry, _ = harness()
    listed = await call(registry, LIST_RELATIONS, context())
    assert isinstance(listed.outcome, ToolSucceeded)
    out = listed.outcome.output
    assert isinstance(out, ListRelationsOutput)
    assert {r.name for r in out.relations} == {
        "sales_items",
        "products",
        "orders",
        "customers",
    }
    described = await call(registry, DESCRIBE_RELATION, context(), name="customers")
    assert isinstance(described.outcome, ToolSucceeded)
    detail = described.outcome.output
    assert isinstance(detail, DescribeRelationOutput)
    assert {f.name: f.type for f in detail.fields} == {
        "customer_ref": "reference",
        "country": "string",
        "state": "string",
        "age_band": "string",
    }


async def test_unknown_relation_and_outage_give_safe_errors() -> None:
    registry, stub = harness()
    bad = await call(registry, DESCRIBE_RELATION, context(), name="users")
    assert isinstance(bad.outcome, ToolFailed)
    assert bad.outcome.code is ToolErrorCode.FIELD_UNAVAILABLE
    assert "users" not in bad.outcome.message
    stub.fail = True
    cold, _ = harness(stub)
    down = await call(cold, LIST_RELATIONS, context())
    assert isinstance(down.outcome, ToolFailed)
    assert down.outcome.code is ToolErrorCode.TEMPORARY_FAILURE
