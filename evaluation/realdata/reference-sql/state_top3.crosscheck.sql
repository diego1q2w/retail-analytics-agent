-- Route B: order-grain pre-aggregation, then state roll-up. Same output columns.
WITH per_order AS (
  SELECT o.order_id, o.user_id, u.state,
    SUM(CASE WHEN oi.status = 'Complete' THEN oi.sale_price ELSE 0 END) AS rev,
    SUM(CASE WHEN oi.status = 'Complete' THEN 1 ELSE 0 END) AS complete_items,
    COUNT(*) AS all_items
  FROM {ds}.orders AS o
  JOIN {ds}.users AS u ON u.id = o.user_id
  JOIN {ds}.order_items AS oi ON oi.order_id = o.order_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'
  GROUP BY o.order_id, o.user_id, u.state),
by_state AS (
  SELECT state, SUM(rev) AS revenue,
    COUNT(DISTINCT CASE WHEN complete_items > 0 THEN user_id END) AS customers
  FROM per_order WHERE state IS NOT NULL GROUP BY state HAVING SUM(complete_items) > 0),
ranked AS (
  SELECT state, revenue, customers, ROW_NUMBER() OVER (ORDER BY revenue DESC, state ASC) AS rk
  FROM by_state)
SELECT
  MAX(CASE WHEN rk = 1 THEN state END) AS state_1,
  ROUND(MAX(CASE WHEN rk = 1 THEN revenue END), 2) AS state_1_revenue,
  MAX(CASE WHEN rk = 1 THEN customers END) AS state_1_customers,
  MAX(CASE WHEN rk = 2 THEN state END) AS state_2,
  ROUND(MAX(CASE WHEN rk = 2 THEN revenue END), 2) AS state_2_revenue,
  MAX(CASE WHEN rk = 2 THEN customers END) AS state_2_customers,
  MAX(CASE WHEN rk = 3 THEN state END) AS state_3,
  ROUND(MAX(CASE WHEN rk = 3 THEN revenue END), 2) AS state_3_revenue,
  MAX(CASE WHEN rk = 3 THEN customers END) AS state_3_customers,
  ROUND((SELECT SUM(rev) FROM per_order), 2) AS total_revenue,
  (SELECT SUM(all_items) FROM per_order) AS window_items_all_statuses
FROM ranked;
