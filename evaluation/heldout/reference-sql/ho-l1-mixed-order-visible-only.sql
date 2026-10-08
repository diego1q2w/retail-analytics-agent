-- Reference for scenario ho-l1-mixed-order-visible-only. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
-- Scope 201-204. Orders are counted from permitted completed items only; the
-- whole-basket amount of the same orders is a different, unpermitted number.
SELECT COUNT(DISTINCT oi.order_id) AS visible_orders,
       SUM(oi.sale_price) AS completed_amount,
       SUM(oi.sale_price) / COUNT(DISTINCT oi.order_id) AS average_order_value
FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
WHERE oi.status = 'Complete' AND oi.product_id IN (201, 202, 203, 204)
  AND o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00';
