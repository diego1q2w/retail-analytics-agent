-- Reference for scenario ho-l1-month-boundary-august. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
SELECT SUM(oi.sale_price) AS revenue, COUNT(*) AS completed_items
FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
WHERE oi.status = 'Complete'
  AND o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-09-01 00:00:00';
