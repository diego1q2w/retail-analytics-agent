"""Approved schema context: permission-scoped, revalidated, bounded, fail-closed."""

from __future__ import annotations

import pytest

from retail_analytics.application.contracts.schema_context import SchemaContextStatus
from retail_analytics.application.discovery import DiscoveryService, SourceSchemaCache
from retail_analytics.application.schema_context import ApprovedSchemaContext
from retail_analytics.domain.access import Permission
from retail_analytics.domain.catalog import SourceType
from retail_analytics.domain.context import ContextBudget
from retail_analytics.domain.preferences import PreferenceKind, PreferenceSetting
from tests.unit.context.support import NARROW, A, B, World
from tests.unit.discovery_fixtures import CATALOG, FakeClock, StubMetadata, context
from tests.unit.privacy.support import EXEC_A, EXEC_B

pytestmark = pytest.mark.asyncio

# Raw warehouse names and personal columns that must never reach the model.
_SOURCE_NAMES = (
    "order_items",
    "sale_price",
    "retail_price",
    "first_name",
    "email",
    "user_id",
    "inventory_item_id",
    "opaque_reference",
)


class _Env:
    def __init__(self, *, max_entries: int = 256) -> None:
        self.clock = FakeClock()
        self.metadata = StubMetadata()
        cache = SourceSchemaCache(CATALOG, self.metadata, self.clock)
        self.discovery = DiscoveryService(CATALOG, cache, self.clock)
        self.schema = ApprovedSchemaContext(self.discovery, max_entries=max_entries)


def _text(lines: tuple[str, ...]) -> str:
    return "\n".join(lines)


async def test_describes_only_sanitized_approved_content() -> None:
    env = _Env()
    brief = await env.schema.for_context(context(products=frozenset({"17", "42"})))
    text = _text(brief.lines)
    assert brief.status is SchemaContextStatus.AVAILABLE
    for relation in ("sales_items", "products", "orders", "customers"):
        assert relation in text
    assert "sale_amount (number)" in text
    assert "product_id = products.product_id" in text
    assert "completed_item_sales v1 = SUM(sale_amount)" in text
    assert "item_status = 'Complete'" in text
    for raw in _SOURCE_NAMES:
        assert raw not in text, raw
    # Neither the product scope nor its version is described.
    assert "17" not in text and "42" not in text


async def test_two_executives_with_different_products_never_share_an_entry() -> None:
    env = _Env()
    a = context(executive="exec-a", products=frozenset({"1", "2"}))
    b = context(executive="exec-b", products=frozenset({"3"}))
    first_a = await env.schema.for_context(a)
    first_b = await env.schema.for_context(b)
    assert env.schema.size == 2
    # Repeat requests (follow-ups) hit each executive's own entry.
    assert await env.schema.for_context(a) is first_a
    assert await env.schema.for_context(b) is first_b
    assert first_a is not first_b
    # Metadata was read once for both executives and all follow-ups.
    assert env.metadata.calls == 1

    # B loses analysis access: B gets nothing, even though A's entry is warm.
    revoked = context(
        executive="exec-b", products=frozenset({"3"}), permissions=frozenset()
    )
    denied = await env.schema.for_context(revoked)
    assert denied.status is SchemaContextStatus.NO_ACCESS
    assert "sales_items" not in _text(denied.lines)
    empty = await env.schema.for_context(
        context(executive="exec-b", products=frozenset())
    )
    assert empty.status is SchemaContextStatus.NO_ACCESS
    assert await env.schema.for_context(a) is first_a


async def test_scope_change_is_a_new_entry_never_a_reused_one() -> None:
    env = _Env()
    before = await env.schema.for_context(context(products=frozenset({"1", "2"})))
    narrowed = await env.schema.for_context(
        context(products=frozenset({"1"}), version=2)
    )
    assert narrowed is not before and env.schema.size == 2
    # Same described content: the run is not restarted for the schema alone
    # (the authority part of the history key already covers the scope change).
    assert narrowed.fingerprint == before.fingerprint
    extra = await env.schema.for_context(
        context(
            products=frozenset({"1"}),
            version=2,
            permissions=frozenset(
                {Permission.ANALYSIS_READ.value, Permission.REPORTS_READ_OWN.value}
            ),
        )
    )
    assert extra is not narrowed


async def test_drift_withholds_the_field_and_its_metrics_before_the_next_use() -> None:
    env = _Env()
    before = await env.schema.for_context(context())
    env.metadata.retype("order_items", "sale_price", SourceType.STRING)
    env.discovery._cache.invalidate()  # what a schema-mismatch error does
    after = await env.schema.for_context(context())
    text = _text(after.lines)
    assert after.fingerprint != before.fingerprint
    assert "temporarily unavailable: sale_amount" in text
    assert "sale_amount (number)" not in text
    assert "completed_item_sales" not in text and "average_order_sales" not in text
    # Metrics that do not need the field remain.
    assert "completed_orders v1" in text


async def test_metadata_outage_is_stated_then_fails_closed() -> None:
    env = _Env()
    fresh = await env.schema.for_context(context())
    env.metadata.fail = True
    env.clock.advance(hours=2)
    stale = await env.schema.for_context(context())
    assert stale.stale and "could not be refreshed" in _text(stale.lines)
    assert stale.fingerprint == fresh.fingerprint
    env.clock.advance(hours=30)
    gone = await env.schema.for_context(context())
    assert gone.status is SchemaContextStatus.UNAVAILABLE
    assert "sales_items" not in _text(gone.lines)
    assert gone.fingerprint != fresh.fingerprint
    assert env.schema.size == 0  # nothing earlier can be served instead


async def test_size_and_entries_are_bounded() -> None:
    env = _Env(max_entries=3)
    full = await env.schema.for_context(context())
    compact = await env.schema.for_context(context(), max_chars=1_500)
    assert compact.chars <= 1_500 < full.chars
    assert "sales_items" in _text(compact.lines) and compact.omitted_relations == 0
    tiny = await env.schema.for_context(context(), max_chars=400)
    assert tiny.chars <= 400 and tiny.omitted_relations > 0
    assert "omitted for space" in _text(tiny.lines)
    for i in range(5):
        await env.schema.for_context(context(executive=f"exec-{i}"))
    assert env.schema.size == 3


# --- the model context -----------------------------------------------------


def _world(**kwargs: object) -> tuple[World, _Env]:
    env = _Env()
    return World(schema=env.schema, **kwargs), env  # type: ignore[arg-type]


async def test_follow_up_context_carries_the_schema_without_rediscovery() -> None:
    w, env = _world()
    first = await w.builder.build(A, w.new_run(), "Revenue in September 2026?")
    follow = await w.builder.build(A, w.new_run(), "And August?")
    assert first.schema is not None and follow.schema is first.schema
    assert env.metadata.calls == 1
    rendered = follow.render(can_describe_schema=True)
    assert rendered.index("<approved_schema>") < rendered.index("<request>")
    assert "SUM(sale_amount)" in rendered
    assert "describe_relation" in rendered
    # The schema counts against the existing context budget.
    plain = World()
    without = await plain.builder.build(A, plain.new_run(), "And August?")
    assert follow.estimated_tokens > without.estimated_tokens


async def test_tool_names_only_when_the_catalog_has_them() -> None:
    w, _ = _world()
    ctx = await w.builder.build(A, w.new_run(), "Revenue in September?")
    plain = ctx.render()
    assert "<approved_schema>" in plain
    assert "describe_relation" not in plain and "list_relations" not in plain


async def test_each_executive_gets_only_their_own_current_schema() -> None:
    w, env = _world()
    a = await w.builder.build(A, w.new_run(), "Revenue?")
    b = await w.builder.build(B, w.new_run(EXEC_B, "s-b"), "Revenue?")
    assert a.schema is not None and b.schema is not None
    assert a.schema is not b.schema and env.schema.size == 2
    # A's entitlement is withdrawn entirely between attempts.
    w.set_products(EXEC_A, frozenset())
    revoked = await w.builder.build(A, w.new_run(), "Revenue?")
    assert revoked.schema is not None
    assert revoked.schema.status is SchemaContextStatus.NO_ACCESS
    assert "sales_items" not in revoked.render()


async def test_narrowed_scope_still_gets_schema_from_a_new_entry() -> None:
    w, env = _world()
    run = w.new_run()
    before = await w.builder.build(A, run, "Revenue?")
    w.set_products(EXEC_A, NARROW)
    after = await w.builder.build(A, run, "Revenue?")
    assert before.schema is not None and after.schema is not None
    assert after.schema is not before.schema and env.schema.size == 2
    assert after.authorization_version != before.authorization_version


async def test_schema_stays_within_a_quarter_of_the_budget() -> None:
    small = ContextBudget(max_tokens=600)
    w, _ = _world(budget=small)
    ctx = await w.builder.build(A, w.new_run(), "Revenue?")
    assert ctx.schema is not None
    assert ctx.schema.chars <= small.max_chars // 4


async def test_effective_preferences_are_stated_as_current_including_defaults() -> None:
    w, _ = _world()
    empty = await w.builder.build(A, w.new_run(), "Revenue?")
    assert "No saved settings apply: defaults are in force" in empty.render()
    await w.preferences.remember(
        A, PreferenceSetting(PreferenceKind.TIME_ZONE, "Europe/Warsaw")
    )
    changed = await w.builder.build(A, w.new_run(), "Revenue?")
    rendered = changed.render()
    assert "Current effective settings, already applied" in rendered
    assert "time_zone = Europe/Warsaw" in rendered
    # Changed analytical preferences still change the history key input.
    assert changed.preferences != empty.preferences
    assert "inspect_preferences" not in rendered
