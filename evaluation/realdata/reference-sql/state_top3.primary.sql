-- Route A: item-grain join. Top three states by completed revenue.
-- Raw source tables, UTC, half-open window on orders.created_at. Aggregates only.
WITH base AS (
  SELECT oi.status, oi.sale_price, o.user_id, u.state
  FROM {ds}.order_items AS oi
  JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  JOIN {ds}.users AS u ON u.id = o.user_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'),
by_state AS (
  SELECT state, SUM(sale_price) AS revenue, COUNT(DISTINCT user_id) AS customers
  FROM base WHERE status = 'Complete' AND state IS NOT NULL GROUP BY state),
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
  ROUND((SELECT SUM(CASE WHEN status = 'Complete' THEN sale_price END) FROM base), 2) AS total_revenue,
  (SELECT COUNT(*) FROM base) AS window_items_all_statuses
FROM ranked;
