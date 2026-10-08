-- Reference for scenario ho-l2-sparse-comparison. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
SELECT SUM(CASE WHEN o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-09-01 00:00:00' THEN oi.sale_price ELSE 0 END) AS august_revenue,
       SUM(CASE WHEN o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00' THEN oi.sale_price ELSE 0 END) AS september_revenue,
       COUNT(CASE WHEN o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-09-01 00:00:00' THEN 1 END) AS august_items,
       COUNT(CASE WHEN o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00' THEN 1 END) AS september_items
FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
WHERE oi.status = 'Complete' AND oi.product_id = 204;
