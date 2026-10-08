-- Reference for scenario ho-l1-empty-result-explained. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
-- Product 207 only has a cancelled item: a genuine zero, not a missing value.
SELECT COALESCE(SUM(oi.sale_price), 0) AS revenue, COUNT(*) AS qualifying_items
FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
WHERE oi.status = 'Complete' AND oi.product_id = 207
  AND o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00';
