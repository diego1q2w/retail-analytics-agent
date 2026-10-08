-- Reference for scenario ho-l1-revenue-north-sept. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
-- Scope: products 201-204. Revenue = Complete items only.
SELECT SUM(oi.sale_price) AS revenue
FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
WHERE oi.status = 'Complete' AND oi.product_id IN (201, 202, 203, 204)
  AND o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00';
