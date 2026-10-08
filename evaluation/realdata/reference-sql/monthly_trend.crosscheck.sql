-- Route B: group by calendar month key (year * 100 + month) and attach to the
-- expected month keys. Same output columns.
WITH keys AS (
  SELECT 1 AS idx, {m1_key} AS ym UNION ALL SELECT 2, {m2_key} UNION ALL SELECT 3, {m3_key}
  UNION ALL SELECT 4, {m4_key} UNION ALL SELECT 5, {m5_key} UNION ALL SELECT 6, {m6_key}),
per_month AS (
  SELECT EXTRACT(YEAR FROM o.created_at) * 100 + EXTRACT(MONTH FROM o.created_at) AS ym,
    SUM(CASE WHEN oi.status = 'Complete' THEN oi.sale_price ELSE 0 END) AS rev,
    COUNT(*) AS all_items
  FROM {ds}.orders AS o JOIN {ds}.order_items AS oi ON oi.order_id = o.order_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{m1} 00:00:00' AND o.created_at < TIMESTAMP '{m7} 00:00:00'
  GROUP BY 1),
months AS (
  SELECT k.idx, COALESCE(m.rev, 0) AS rev, COALESCE(m.all_items, 0) AS all_items
  FROM keys AS k LEFT JOIN per_month AS m ON m.ym = k.ym),
ranked AS (SELECT idx, rev, all_items, ROW_NUMBER() OVER (ORDER BY rev DESC, idx ASC) AS rk FROM months)
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
  SUM(all_items) AS window_items_all_statuses
FROM ranked;
