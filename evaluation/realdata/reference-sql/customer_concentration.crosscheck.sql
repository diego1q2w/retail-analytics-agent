-- Route B: top ten by ORDER BY ... LIMIT, totals straight from items.
WITH window_items AS (
  SELECT oi.status, oi.sale_price, o.user_id
  FROM {ds}.orders AS o JOIN {ds}.order_items AS oi ON oi.order_id = o.order_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'),
per_customer AS (
  SELECT user_id, SUM(sale_price) AS rev FROM window_items WHERE status = 'Complete' GROUP BY user_id),
top10 AS (SELECT rev FROM per_customer ORDER BY rev DESC, user_id ASC LIMIT 10),
top1 AS (SELECT rev FROM per_customer ORDER BY rev DESC, user_id ASC LIMIT 1),
totals AS (SELECT SUM(CASE WHEN status = 'Complete' THEN sale_price END) AS total,
                  COUNT(DISTINCT CASE WHEN status = 'Complete' THEN user_id END) AS customers,
                  COUNT(*) AS all_items FROM window_items)
SELECT
  (SELECT customers FROM totals) AS customers_total,
  ROUND((SELECT total FROM totals), 2) AS total_revenue,
  ROUND((SELECT SUM(rev) FROM top10), 2) AS top10_revenue,
  ROUND((SELECT SUM(rev) FROM top10) / (SELECT total FROM totals), 4) AS top10_share,
  ROUND((SELECT rev FROM top1) / (SELECT total FROM totals), 4) AS top1_share,
  (SELECT all_items FROM totals) AS window_items_all_statuses;
