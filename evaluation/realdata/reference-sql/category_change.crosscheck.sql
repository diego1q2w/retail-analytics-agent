-- Route B: each window aggregated on its own, then joined by category.
WITH w1 AS (
  SELECT p.category, SUM(oi.sale_price) AS rev
  FROM {ds}.order_items AS oi JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  JOIN {ds}.products AS p ON p.id = oi.product_id
  WHERE oi.status = 'Complete' AND oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_mid} 00:00:00'
  GROUP BY p.category),
w2 AS (
  SELECT p.category, SUM(oi.sale_price) AS rev
  FROM {ds}.order_items AS oi JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  JOIN {ds}.products AS p ON p.id = oi.product_id
  WHERE oi.status = 'Complete' AND oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_mid} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'
  GROUP BY p.category),
cats AS (SELECT category FROM w1 UNION SELECT category FROM w2),
joined AS (
  SELECT c.category, COALESCE(w1.rev, 0) AS first_rev, COALESCE(w2.rev, 0) AS second_rev
  FROM cats AS c LEFT JOIN w1 ON w1.category = c.category LEFT JOIN w2 ON w2.category = c.category
  WHERE c.category IS NOT NULL),
gain AS (SELECT category, second_rev - first_rev AS delta, ROW_NUMBER() OVER (ORDER BY second_rev - first_rev DESC, category ASC) AS rk FROM joined),
weak AS (SELECT category, second_rev - first_rev AS delta, ROW_NUMBER() OVER (ORDER BY second_rev - first_rev ASC, category ASC) AS rk FROM joined)
SELECT
  ROUND((SELECT SUM(first_rev) FROM joined), 2) AS first_period_revenue,
  ROUND((SELECT SUM(second_rev) FROM joined), 2) AS second_period_revenue,
  ROUND((SELECT SUM(second_rev) - SUM(first_rev) FROM joined), 2) AS revenue_change,
  (SELECT category FROM gain WHERE rk = 1) AS gain_1_category,
  ROUND((SELECT delta FROM gain WHERE rk = 1), 2) AS gain_1_delta,
  (SELECT category FROM gain WHERE rk = 2) AS gain_2_category,
  ROUND((SELECT delta FROM gain WHERE rk = 2), 2) AS gain_2_delta,
  (SELECT category FROM weak WHERE rk = 1) AS weakest_category,
  ROUND((SELECT delta FROM weak WHERE rk = 1), 2) AS weakest_delta,
  (SELECT COUNT(*) FROM {ds}.order_items AS oi JOIN {ds}.orders AS o ON o.order_id = oi.order_id
   WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
     AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00') AS window_items_all_statuses;
