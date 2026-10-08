-- Route B: aggregate items by product id first (orders only as an IN filter),
-- then attach names. Same output columns.
WITH window_items AS (
  SELECT oi.product_id, oi.status, oi.sale_price
  FROM {ds}.order_items AS oi
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND oi.order_id IN (
      SELECT o.order_id FROM {ds}.orders AS o
      WHERE o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00')),
per_product AS (
  SELECT product_id,
    SUM(CASE WHEN status = 'Complete' THEN sale_price ELSE 0 END) AS revenue,
    SUM(CASE WHEN status = 'Complete' THEN 1 ELSE 0 END) AS units,
    COUNT(*) AS all_items
  FROM window_items GROUP BY product_id),
named AS (
  SELECT p.id AS product_id, p.name AS product_name, pp.revenue, pp.units
  FROM per_product AS pp JOIN {ds}.products AS p ON p.id = pp.product_id
  WHERE pp.units > 0),
ranked AS (
  SELECT product_name, revenue, units, ROW_NUMBER() OVER (ORDER BY revenue DESC, product_id ASC) AS rk
  FROM named)
SELECT
  MAX(CASE WHEN rk = 1 THEN product_name END) AS product_1,
  ROUND(MAX(CASE WHEN rk = 1 THEN revenue END), 2) AS product_1_revenue,
  MAX(CASE WHEN rk = 1 THEN units END) AS product_1_units,
  MAX(CASE WHEN rk = 2 THEN product_name END) AS product_2,
  ROUND(MAX(CASE WHEN rk = 2 THEN revenue END), 2) AS product_2_revenue,
  MAX(CASE WHEN rk = 2 THEN units END) AS product_2_units,
  MAX(CASE WHEN rk = 3 THEN product_name END) AS product_3,
  ROUND(MAX(CASE WHEN rk = 3 THEN revenue END), 2) AS product_3_revenue,
  MAX(CASE WHEN rk = 3 THEN units END) AS product_3_units,
  ROUND((SELECT SUM(revenue) FROM per_product), 2) AS total_revenue,
  (SELECT SUM(all_items) FROM per_product) AS window_items_all_statuses
FROM ranked;
