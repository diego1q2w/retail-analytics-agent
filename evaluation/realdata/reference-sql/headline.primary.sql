-- Route A: item-grain join. Headline completed sales for a product scope.
WITH base AS (
  SELECT oi.status, oi.sale_price, o.user_id
  FROM {ds}.order_items AS oi
  JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00')
SELECT
  ROUND(SUM(CASE WHEN status = 'Complete' THEN sale_price END), 2) AS revenue,
  COUNT(CASE WHEN status = 'Complete' THEN 1 END) AS completed_items,
  COUNT(DISTINCT CASE WHEN status = 'Complete' THEN user_id END) AS distinct_customers,
  COUNT(CASE WHEN status = 'Returned' THEN 1 END) AS returned_items,
  ROUND(SUM(CASE WHEN status = 'Returned' THEN sale_price END), 2) AS returned_value,
  COUNT(*) AS window_items_all_statuses
FROM base;
