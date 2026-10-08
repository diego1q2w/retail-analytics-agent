-- Route A: completed revenue by product category in two consecutive windows
-- [w_start, w_mid) and [w_mid, w_end), and the change per category.
WITH base AS (
  SELECT oi.status, oi.sale_price, p.category, o.created_at
  FROM {ds}.order_items AS oi
  JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  JOIN {ds}.products AS p ON p.id = oi.product_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'),
by_cat AS (
  SELECT category,
    SUM(CASE WHEN status = 'Complete' AND created_at < TIMESTAMP '{w_mid} 00:00:00' THEN sale_price ELSE 0 END) AS first_rev,
    SUM(CASE WHEN status = 'Complete' AND created_at >= TIMESTAMP '{w_mid} 00:00:00' THEN sale_price ELSE 0 END) AS second_rev
  FROM base WHERE category IS NOT NULL GROUP BY category
  HAVING SUM(CASE WHEN status = 'Complete' THEN 1 ELSE 0 END) > 0),
deltas AS (SELECT category, second_rev - first_rev AS delta FROM by_cat),
gain AS (SELECT category, delta, ROW_NUMBER() OVER (ORDER BY delta DESC, category ASC) AS rk FROM deltas),
weak AS (SELECT category, delta, ROW_NUMBER() OVER (ORDER BY delta ASC, category ASC) AS rk FROM deltas)
SELECT
  ROUND((SELECT SUM(first_rev) FROM by_cat), 2) AS first_period_revenue,
  ROUND((SELECT SUM(second_rev) FROM by_cat), 2) AS second_period_revenue,
  ROUND((SELECT SUM(second_rev) - SUM(first_rev) FROM by_cat), 2) AS revenue_change,
  (SELECT category FROM gain WHERE rk = 1) AS gain_1_category,
  ROUND((SELECT delta FROM gain WHERE rk = 1), 2) AS gain_1_delta,
  (SELECT category FROM gain WHERE rk = 2) AS gain_2_category,
  ROUND((SELECT delta FROM gain WHERE rk = 2), 2) AS gain_2_delta,
  (SELECT category FROM weak WHERE rk = 1) AS weakest_category,
  ROUND((SELECT delta FROM weak WHERE rk = 1), 2) AS weakest_delta,
  (SELECT COUNT(*) FROM base) AS window_items_all_statuses;
