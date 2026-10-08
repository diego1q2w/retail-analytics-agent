-- Route A: rank customers by completed revenue; aggregates only (no customer rows leave the query).
WITH per_customer AS (
  SELECT o.user_id, SUM(oi.sale_price) AS rev
  FROM {ds}.order_items AS oi JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  WHERE oi.status = 'Complete' AND oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'
  GROUP BY o.user_id),
ranked AS (SELECT rev, ROW_NUMBER() OVER (ORDER BY rev DESC, user_id ASC) AS rk FROM per_customer)
SELECT
  COUNT(*) AS customers_total,
  ROUND(SUM(rev), 2) AS total_revenue,
  ROUND(SUM(CASE WHEN rk <= 10 THEN rev END), 2) AS top10_revenue,
  ROUND(SUM(CASE WHEN rk <= 10 THEN rev END) / SUM(rev), 4) AS top10_share,
  ROUND(MAX(CASE WHEN rk = 1 THEN rev END) / SUM(rev), 4) AS top1_share,
  (SELECT COUNT(*) FROM {ds}.order_items AS oi JOIN {ds}.orders AS o ON o.order_id = oi.order_id
   WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
     AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00') AS window_items_all_statuses
FROM ranked;
