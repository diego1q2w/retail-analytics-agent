-- Route A: 5-year bands via FLOOR(age / 5). Total and per-customer completed spend.
WITH base AS (
  SELECT oi.sale_price, o.user_id, CAST(FLOOR(u.age / 5.0) AS BIGINT) * 5 AS band
  FROM {ds}.order_items AS oi
  JOIN {ds}.orders AS o ON o.order_id = oi.order_id
  JOIN {ds}.users AS u ON u.id = o.user_id
  WHERE oi.status = 'Complete' AND oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'),
by_band AS (
  SELECT band, SUM(sale_price) AS spend, COUNT(DISTINCT user_id) AS customers FROM base GROUP BY band),
per_cust AS (SELECT band, spend, customers, spend / customers AS per_customer FROM by_band),
r_total AS (SELECT band, spend, ROW_NUMBER() OVER (ORDER BY spend DESC, band ASC) AS rk FROM per_cust),
r_per AS (SELECT band, per_customer, customers, ROW_NUMBER() OVER (ORDER BY per_customer DESC, band ASC) AS rk FROM per_cust)
SELECT
  (SELECT CONCAT(CAST(band AS STRING), '-', CAST(band + 4 AS STRING)) FROM r_total WHERE rk = 1) AS top_total_band,
  ROUND((SELECT spend FROM r_total WHERE rk = 1), 2) AS top_total_band_spend,
  (SELECT CONCAT(CAST(band AS STRING), '-', CAST(band + 4 AS STRING)) FROM r_per WHERE rk = 1) AS top_per_customer_band,
  ROUND((SELECT per_customer FROM r_per WHERE rk = 1), 2) AS top_per_customer_amount,
  (SELECT customers FROM r_per WHERE rk = 1) AS top_per_customer_band_customers,
  ROUND((SELECT SUM(spend) FROM by_band), 2) AS total_revenue,
  (SELECT COUNT(*) FROM {ds}.order_items AS oi JOIN {ds}.orders AS o ON o.order_id = oi.order_id
   WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
     AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00') AS window_items_all_statuses;
