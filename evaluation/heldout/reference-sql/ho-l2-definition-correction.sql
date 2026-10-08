-- Reference for scenario ho-l2-definition-correction. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
-- Scope 201-204, September. Default definition and the user's scoped correction.
SELECT SUM(CASE WHEN oi.status = 'Complete' THEN oi.sale_price ELSE 0 END) AS revenue_default_definition,
       SUM(CASE WHEN oi.status IN ('Complete', 'Shipped') THEN oi.sale_price ELSE 0 END) AS revenue_including_shipped
FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
WHERE oi.product_id IN (201, 202, 203, 204)
  AND o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00';
