-- Route A: item-grain join to products. Top three products by completed revenue.
WITH base AS (
  SELECT oi.status, oi.sale_price, p.id AS product_id, p.name AS product_name
  FROM {ds}.order_items AS oi
  JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  JOIN {ds}.products AS p ON p.id = oi.product_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'),
by_product AS (
  SELECT product_id, MAX(product_name) AS product_name, SUM(sale_price) AS revenue, COUNT(*) AS units
  FROM base WHERE status = 'Complete' GROUP BY product_id),
ranked AS (
  SELECT product_name, revenue, units, ROW_NUMBER() OVER (ORDER BY revenue DESC, product_id ASC) AS rk
  FROM by_product)
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
  ROUND((SELECT SUM(CASE WHEN status = 'Complete' THEN sale_price END) FROM base), 2) AS total_revenue,
  (SELECT COUNT(*) FROM base) AS window_items_all_statuses
FROM ranked;
