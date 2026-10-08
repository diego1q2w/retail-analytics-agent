-- Route B: per-customer pre-aggregation, then totals. Same output columns.
WITH per_customer AS (
  SELECT o.user_id,
    SUM(CASE WHEN oi.status = 'Complete' THEN oi.sale_price ELSE 0 END) AS rev,
    SUM(CASE WHEN oi.status = 'Complete' THEN 1 ELSE 0 END) AS complete_items,
    SUM(CASE WHEN oi.status = 'Returned' THEN 1 ELSE 0 END) AS returned_items,
    SUM(CASE WHEN oi.status = 'Returned' THEN oi.sale_price ELSE 0 END) AS returned_value,
    COUNT(*) AS all_items
  FROM {ds}.orders AS o JOIN {ds}.order_items AS oi ON oi.order_id = o.order_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'
  GROUP BY o.user_id)
SELECT
  ROUND(SUM(rev), 2) AS revenue,
  SUM(complete_items) AS completed_items,
  COUNT(CASE WHEN complete_items > 0 THEN 1 END) AS distinct_customers,
  SUM(returned_items) AS returned_items,
  ROUND(SUM(returned_value), 2) AS returned_value,
  SUM(all_items) AS window_items_all_statuses
FROM per_customer;
