# Source data profile

Generated 2026-10-08T16:51:36Z against `bigquery-public-data.thelook_ecommerce` (US). Aggregates only; no personal values.

## Tables

| table | rows | columns |
| --- | ---: | --- |
| orders | 124987 | 9 |
| order_items | 181162 | 11 |
| products | 29120 | 9 |
| users | 100000 | 16 |

## Findings

- Item status values: Cancelled (26917), Complete (45283), Processing (36363), Returned (18080), Shipped (54519)
- Item `sale_price`: FLOAT, nulls 0, min 0.02, max 999.00, negative 0, zero 0.
- Items: 181162 rows, 181162 distinct ids (one row per item).
- Orders: 124987 rows, 124987 distinct; created 2019-01-09 15:50:38+00 to 2026-10-08 00:39:26.922376+00 UTC; future-dated orders 0.
- Item `created_at` ranges to 2026-10-11 23:58:52.640285+00; 1499 items are future-dated (the logical `ordered_date` uses `orders.created_at`, so it is not affected).
- Items whose date differs from their order date: 81401 (confirms the order date, not the item date, must be used).
- Orphans: items without order 0, without product 0, without user 0; orders without user 0, without items 0.
- `orders.num_of_item` differs from item rows for 0 orders.
- Products: 29120 rows, ids 1-29120; nulls name 2, brand 24.
- Departments: Men ids 15990-29120, Women ids 1-15989
- Users: 100000 rows, ages 12-70, null age 0, 16 countries, 227 states.
- Catalog mapping issues against live metadata: 0.
- Currency: UNKNOWN (no currency column, label or description in the source metadata).

## Analyses run through the compiler (real BigQuery jobs)

- `completed_sales_by_category`: job `ra_t34_67f459d8e2a048bfbdbecbd28ccc4101`, fingerprint `b1723a7310a100ee12cbb425a02e7e30`, processed 0 bytes (cache hit True), window [2026-01-01, 2026-10-01), 5 rows, scope products 1-15989 (demo executive A).
- `completed_sales_by_age_band`: job `ra_t34_d6e24067ab68470aa315a02818617031`, fingerprint `7accbaba2b692069d23145697a1ea132`, processed 11253720 bytes (cache hit False), window [2026-01-01, 2026-10-01), 13 rows, scope products 1-15989 (demo executive A).
- Physical cross-check of the same population: {'completed_item_sales': 385383.3504784107, 'completed_items': 6775, 'customers': 4382} (0 bytes billed); age-band sales match True, customers match True.

Profile queries billed 22020096 bytes in total.
