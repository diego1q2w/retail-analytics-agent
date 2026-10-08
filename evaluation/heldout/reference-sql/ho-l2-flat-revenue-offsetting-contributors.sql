-- Reference for scenario ho-l2-flat-revenue-offsetting-contributors. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
WITH by_product AS (
  SELECT p.name,
         SUM(CASE WHEN o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00' THEN oi.sale_price ELSE 0 END)
       - SUM(CASE WHEN o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-09-01 00:00:00' THEN oi.sale_price ELSE 0 END) AS delta
  FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id JOIN thelook.products AS p ON p.id = oi.product_id
  WHERE oi.status = 'Complete'
  GROUP BY 1)
SELECT (SELECT SUM(oi.sale_price) FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id WHERE oi.status = 'Complete'
          AND o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-09-01 00:00:00') AS august_revenue,
       (SELECT SUM(oi.sale_price) FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id WHERE oi.status = 'Complete'
          AND o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-01 00:00:00') AS september_revenue,
       (SELECT SUM(delta) FROM by_product) AS revenue_change,
       (SELECT name FROM by_product ORDER BY delta ASC LIMIT 1) AS largest_decline_product,
       (SELECT MIN(delta) FROM by_product) AS largest_decline_amount,
       (SELECT name FROM by_product ORDER BY delta DESC LIMIT 1) AS largest_gain_product,
       (SELECT MAX(delta) FROM by_product) AS largest_gain_amount;
