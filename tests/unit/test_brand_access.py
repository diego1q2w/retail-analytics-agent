"""Brand-based manager access without a database (T05-F2).

The PostgreSQL resolution, versioning and audit are covered by
``tests/integration/test_brand_access.py`` (Docker).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from retail_analytics.adapters.bigquery.product_brands import (
    BYTES_CAP,
    BigQueryProductBrands,
    product_brands_sql,
)
from retail_analytics.adapters.evaluation.fixture_warehouse import (
    frozen_extract_warehouse,
    heldout_fixture_warehouse,
)
from retail_analytics.application.brand_access import (
    BrandAccessError,
    BrandAccessService,
    ProductBrandsUnavailable,
)
from retail_analytics.application.contracts.brand_access import (
    BrandCatalogSync,
    ProductBrandCatalog,
)
from retail_analytics.bootstrap import dev_access, local_setup
from retail_analytics.bootstrap.local_setup import SetupContext
from retail_analytics.domain.access import (
    ExecutiveAccess,
    Role,
    branded_products,
    is_valid_brand,
    product_brand_digest,
)

ROOT = Path(__file__).resolve().parents[2]


# -- domain -------------------------------------------------------------------


@pytest.mark.parametrize("brand", ["Levi's", "Calvin Klein", "7 For All Mankind"])
def test_valid_brands(brand: str) -> None:
    assert is_valid_brand(brand)


@pytest.mark.parametrize("brand", ["", " ", " Nike", "Nike ", "a\nb", "x" * 201])
def test_invalid_brands(brand: str) -> None:
    assert not is_valid_brand(brand)


def test_catalog_rows_keep_exact_brands_and_count_unusable_ones() -> None:
    brands, skipped = branded_products(
        [("1", "Hugo Boss"), ("2", "HUGO BOSS"), ("3", None), ("4", "  "), ("5", "")]
    )
    # Case variants stay distinct brands: no normalization, no fuzzy match.
    assert brands == {"1": "Hugo Boss", "2": "HUGO BOSS"}
    assert skipped == 3
    with pytest.raises(ValueError, match="twice"):
        branded_products([("1", "A"), ("1", "B")])
    with pytest.raises(ValueError, match="invalid product"):
        branded_products([("01", "A")])


def test_catalog_digest_is_order_independent_and_brand_sensitive() -> None:
    a = product_brand_digest({"1": "A", "10": "B", "2": "A"})
    assert a == product_brand_digest({"2": "A", "1": "A", "10": "B"})
    assert a != product_brand_digest({"1": "A", "10": "B", "2": "B"})


def test_catalog_contract_refuses_invalid_rows() -> None:
    with pytest.raises(ValueError):
        ProductBrandCatalog({"1": " A"}, "src")
    with pytest.raises(ValueError):
        ProductBrandCatalog({"x": "A"}, "src")
    with pytest.raises(ValueError):
        ProductBrandCatalog({}, "")


# -- local sources ------------------------------------------------------------


@pytest.mark.asyncio
async def test_heldout_fixture_has_brands_and_frozen_extract_has_none() -> None:
    heldout = heldout_fixture_warehouse(ROOT / "evaluation/heldout/fixture")
    catalog = await heldout.read_product_brands()
    assert set(catalog.brands.values()) == {"Aster", "Birch", "Cedar", "Dune", "Ember"}
    assert catalog.source_ref == "heldout-fixture-1"

    extract = frozen_extract_warehouse(
        ROOT / "evaluation/realdata/extract", "thelook-realdata-extract-1"
    )
    frozen = await extract.read_product_brands()
    # The sanitized extract never selected brands: nothing is brand-grantable.
    assert frozen.brands == {}
    assert frozen.products_without_brand > 16000


class _Job:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def result(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Client:
    def __init__(self, rows: list[tuple[Any, ...]], error: Exception | None) -> None:
        self.rows, self.error = rows, error
        self.calls: list[tuple[str, Any]] = []

    def query(self, sql: str, *, job_config: Any, location: str) -> _Job:
        self.calls.append((sql, job_config))
        if self.error is not None:
            raise self.error
        return _Job(self.rows)


@pytest.mark.asyncio
async def test_bigquery_source_is_a_fixed_capped_read() -> None:
    client = _Client([(1, "Levi's"), (2, None), (3, "Carhartt")], None)
    source = BigQueryProductBrands(lambda: client, location="US")  # type: ignore[arg-type,return-value]
    catalog = await source.read_product_brands()
    assert catalog.brands == {"1": "Levi's", "3": "Carhartt"}
    assert catalog.products_without_brand == 1
    sql, config = client.calls[0]
    assert sql == product_brands_sql("bigquery-public-data.thelook_ecommerce")
    assert config.maximum_bytes_billed == BYTES_CAP


@pytest.mark.asyncio
async def test_bigquery_source_errors_do_not_echo_provider_messages() -> None:
    client = _Client([], RuntimeError("secret project detail"))
    source = BigQueryProductBrands(lambda: client, location="US")  # type: ignore[arg-type,return-value]
    with pytest.raises(ProductBrandsUnavailable) as failed:
        await source.read_product_brands()
    assert "secret" not in str(failed.value)


# -- service ------------------------------------------------------------------


class _Store:
    """In-memory store: records the calls; resolution is tested on Postgres."""

    def __init__(self, catalog: Mapping[str, str]) -> None:
        self.catalog = dict(catalog)
        self.assigned: dict[str, frozenset[str]] = {"exec-1": frozenset()}
        self.synced: list[ProductBrandCatalog] = []

    async def brands_of(self, executive_id: str) -> frozenset[str]:
        return self.assigned[executive_id]

    async def replace_brands(
        self, executive_id: str, brands: Iterable[str], *, actor_id: str = ""
    ) -> ExecutiveAccess:
        self.assigned[executive_id] = frozenset(brands)
        return await self.get(executive_id)  # type: ignore[return-value]

    async def catalog_brand_sizes(self, brands: Iterable[str]) -> Mapping[str, int]:
        values = list(self.catalog.values())
        return {b: values.count(b) for b in brands}

    async def sync_catalog(
        self, catalog: ProductBrandCatalog, *, actor_id: str = ""
    ) -> BrandCatalogSync:
        self.synced.append(catalog)
        return BrandCatalogSync(catalog.digest, catalog.source_ref, 0, 0, 0, 0)

    async def get(self, executive_id: str) -> ExecutiveAccess | None:
        products = frozenset(
            p for p, b in self.catalog.items() if b in self.assigned[executive_id]
        )
        return ExecutiveAccess(
            executive_id, frozenset({Role.EXECUTIVE}), products, True, 1
        )


class _Source:
    def __init__(self, brands: dict[str, str]) -> None:
        self.brands = brands

    async def read_product_brands(self) -> ProductBrandCatalog:
        return ProductBrandCatalog(self.brands, "test")


def _service(store: _Store, source: _Source | None = None) -> BrandAccessService:
    return BrandAccessService(store, store, source)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_assignment_needs_an_exact_catalog_match() -> None:
    store = _Store({"1": "Levi's", "2": "Carhartt"})
    service = _service(store)
    for wrong in ("levi's", "LEVI'S", "Levis", "Nike"):
        with pytest.raises(BrandAccessError) as refused:
            await service.assign("exec-1", [wrong])
        assert refused.value.code == "unknown_brand"
    assert store.assigned["exec-1"] == frozenset()

    result = await service.assign("exec-1", ["Levi's"])
    assert result.brand_products == {"Levi's": 1}
    assert result.access.product_ids == {"1"}

    kept = await service.assign("exec-1", ["Nike"], allow_unmatched=True)
    assert kept.unmatched == {"Nike"} and kept.access.product_ids == {"1"}

    removed = await service.remove("exec-1", ["Levi's", "not assigned"])
    assert removed.brands == {"Nike"} and not removed.access.product_ids


@pytest.mark.asyncio
async def test_invalid_brand_names_are_refused_before_any_change() -> None:
    store = _Store({"1": "Levi's"})
    with pytest.raises(BrandAccessError) as refused:
        await _service(store).replace("exec-1", ["Levi's", " Levi's"])
    assert refused.value.code == "invalid_brand"
    assert store.assigned["exec-1"] == frozenset()


@pytest.mark.asyncio
async def test_sync_needs_a_source_and_refuses_an_empty_catalog() -> None:
    store = _Store({})
    with pytest.raises(ProductBrandsUnavailable):
        await _service(store).sync_catalog()
    with pytest.raises(BrandAccessError) as empty:
        await _service(store, _Source({})).sync_catalog()
    assert empty.value.code == "empty_catalog" and not store.synced
    await _service(store, _Source({"1": "A"})).sync_catalog()
    assert len(store.synced) == 1


# -- local commands and bootstrap ---------------------------------------------


def test_sync_brands_needs_live_mode_with_a_project() -> None:
    result = CliRunner().invoke(
        dev_access.main,
        ["sync-brands"],
        env={"APP_MODE": "fixture", "APP_DATABASE_URL": "postgresql://x@h/db"},
    )
    assert result.exit_code == 1
    assert "no brand catalog source" in result.output


def test_brand_catalog_step_runs_after_credentials_and_skips_fixture(
    tmp_path: Path,
) -> None:
    names = [s.name for s in local_setup.STEPS]
    assert names.index("brand-catalog") > names.index("check-credentials")
    assert names.index("brand-catalog") > names.index("executives")
    env_file = tmp_path / "local.env"
    env_file.write_text("APP_MODE=fixture\n", encoding="utf-8")
    ctx = SetupContext(
        root=tmp_path, env_file=env_file, project="p", echo=lambda _line: None
    )
    ctx.refresh_values()
    result = local_setup.step_brand_catalog(ctx)
    assert result.status == "skipped"
    assert "brand managers see no products" in result.message
