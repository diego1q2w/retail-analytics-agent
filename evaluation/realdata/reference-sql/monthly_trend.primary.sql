-- Route A: six month buckets by timestamp range comparison on orders.created_at.
WITH base AS (
  SELECT oi.status, oi.sale_price, o.created_at
  FROM {ds}.order_items AS oi
  JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{m1} 00:00:00' AND o.created_at < TIMESTAMP '{m7} 00:00:00'),
months AS (
  SELECT 1 AS idx, COALESCE(SUM(CASE WHEN status = 'Complete' AND created_at >= TIMESTAMP '{m1} 00:00:00' AND created_at < TIMESTAMP '{m2} 00:00:00' THEN sale_price END), 0) AS rev FROM base
  UNION ALL SELECT 2, COALESCE(SUM(CASE WHEN status = 'Complete' AND created_at >= TIMESTAMP '{m2} 00:00:00' AND created_at < TIMESTAMP '{m3} 00:00:00' THEN sale_price END), 0) FROM base
  UNION ALL SELECT 3, COALESCE(SUM(CASE WHEN status = 'Complete' AND created_at >= TIMESTAMP '{m3} 00:00:00' AND created_at < TIMESTAMP '{m4} 00:00:00' THEN sale_price END), 0) FROM base
  UNION ALL SELECT 4, COALESCE(SUM(CASE WHEN status = 'Complete' AND created_at >= TIMESTAMP '{m4} 00:00:00' AND created_at < TIMESTAMP '{m5} 00:00:00' THEN sale_price END), 0) FROM base
  UNION ALL SELECT 5, COALESCE(SUM(CASE WHEN status = 'Complete' AND created_at >= TIMESTAMP '{m5} 00:00:00' AND created_at < TIMESTAMP '{m6} 00:00:00' THEN sale_price END), 0) FROM base
  UNION ALL SELECT 6, COALESCE(SUM(CASE WHEN status = 'Complete' AND created_at >= TIMESTAMP '{m6} 00:00:00' AND created_at < TIMESTAMP '{m7} 00:00:00' THEN sale_price END), 0) FROM base),
ranked AS (SELECT idx, rev, ROW_NUMBER() OVER (ORDER BY rev DESC, idx ASC) AS rk FROM months)
SELECT
  ROUND(MAX(CASE WHEN idx = 1 THEN rev END), 2) AS month_1_revenue,
  ROUND(MAX(CASE WHEN idx = 2 THEN rev END), 2) AS month_2_revenue,
  ROUND(MAX(CASE WHEN idx = 3 THEN rev END), 2) AS month_3_revenue,
  ROUND(MAX(CASE WHEN idx = 4 THEN rev END), 2) AS month_4_revenue,
  ROUND(MAX(CASE WHEN idx = 5 THEN rev END), 2) AS month_5_revenue,
  ROUND(MAX(CASE WHEN idx = 6 THEN rev END), 2) AS month_6_revenue,
  ROUND(SUM(CASE WHEN idx <= 3 THEN rev END), 2) AS first_quarter_revenue,
  ROUND(SUM(CASE WHEN idx > 3 THEN rev END), 2) AS second_quarter_revenue,
  ROUND(SUM(CASE WHEN idx > 3 THEN rev END) - SUM(CASE WHEN idx <= 3 THEN rev END), 2) AS quarter_change,
  MAX(CASE WHEN rk = 1 THEN idx END) AS best_month_index,
  (SELECT COUNT(*) FROM base) AS window_items_all_statuses
FROM ranked;
