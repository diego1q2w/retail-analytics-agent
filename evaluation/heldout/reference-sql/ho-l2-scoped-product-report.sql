-- Reference for scenario ho-l2-scoped-product-report. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
-- Scope 201-204, 1 August to 31 October.
WITH per_product AS (
  SELECT p.name, SUM(oi.sale_price) AS amount
  FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id JOIN thelook.products AS p ON p.id = oi.product_id
  WHERE oi.status = 'Complete' AND oi.product_id IN (201, 202, 203, 204)
    AND o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-11-01 00:00:00'
  GROUP BY 1)
SELECT (SELECT name FROM per_product ORDER BY amount DESC LIMIT 1) AS top_product,
       (SELECT MAX(amount) FROM per_product) AS top_product_revenue,
       (SELECT SUM(amount) FROM per_product) AS scope_total_revenue,
       (SELECT MAX(amount) FROM per_product) / (SELECT SUM(amount) FROM per_product) AS top_product_share;
