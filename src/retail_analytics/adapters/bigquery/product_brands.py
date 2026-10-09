"""``ProductBrandSource`` over the warehouse ``products`` table (live).

A trusted, fixed read of ``id`` and ``brand`` (never model or user SQL),
read-only and capped by ``maximum_bytes_billed``. Brands are kept verbatim;
products whose brand is missing or unusable are only counted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Final

from google.cloud import bigquery

from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.brand_access import ProductBrandsUnavailable
from retail_analytics.application.contracts.brand_access import ProductBrandCatalog
from retail_analytics.domain.access import branded_products

BYTES_CAP: Final = 64 * 1024 * 1024


def product_brands_sql(dataset: str) -> str:
    return f"SELECT CAST(id AS STRING) AS product_id, brand FROM `{dataset}.products`"  # noqa: S608


class BigQueryProductBrands:
    def __init__(
        self,
        client_factory: Callable[[], bigquery.Client],
        *,
        location: str,
        dataset: str = PUBLIC_DATASET,
    ) -> None:
        self._client_factory = client_factory
        self._location = location
        self._dataset = dataset

    async def read_product_brands(self) -> ProductBrandCatalog:
        try:
            rows = await asyncio.to_thread(self._read)
            brands, skipped = branded_products(rows)
        except Exception as error:
            # Never echo provider messages; the class is enough to act on.
            raise ProductBrandsUnavailable(
                f"product brands unavailable ({type(error).__name__})"
            ) from None
        return ProductBrandCatalog(
            brands=brands,
            source_ref=f"bigquery:{self._dataset}.products",
            products_without_brand=skipped,
        )

    def _read(self) -> list[tuple[str, str | None]]:
        client = self._client_factory()
        job = client.query(
            product_brands_sql(self._dataset),
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=BYTES_CAP,
                labels={"app": "retail-analytics", "task": "brand-catalog"},
            ),
            location=self._location,
        )
        return [(str(row[0]), row[1]) for row in job.result()]
