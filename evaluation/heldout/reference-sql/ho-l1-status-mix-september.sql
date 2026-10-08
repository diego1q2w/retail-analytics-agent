-- Reference for scenario ho-l1-status-mix-september. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
SELECT SUM(CASE WHEN oi.status = 'Complete' THEN oi.sale_price ELSE 0 END) AS revenue,
       SUM(CASE WHEN oi.status <> 'Complete' THEN oi.sale_price ELSE 0 END) AS not_counted_amount,
       SUM(CASE WHEN oi.status = 'Returned' THEN oi.sale_price ELSE 0 END) AS returned_amount,
       SUM(CASE WHEN oi.status = 'Cancelled' THEN oi.sale_price ELSE 0 END) AS cancelled_amount,
       SUM(CASE WHEN oi.status = 'Shipped' THEN oi.sale_price ELSE 0 END) AS shipped_amount,
       SUM(CASE WHEN oi.status = 'Processing' THEN oi.sale_price ELSE 0 END) AS processing_amount
FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
WHERE o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00';
