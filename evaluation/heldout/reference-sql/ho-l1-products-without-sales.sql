-- Reference for scenario ho-l1-products-without-sales. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
SELECT COUNT(*) AS products_without_completed_sales
FROM thelook.products AS p
WHERE NOT EXISTS (
  SELECT 1
  FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
  WHERE oi.product_id = p.id AND oi.status = 'Complete'
    AND o.created_at >= TIMESTAMP '2026-07-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00');
