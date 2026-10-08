-- Route B: bands via age - MOD(age, 5); customer totals first, then bands.
WITH cust AS (
  SELECT o.user_id, SUM(oi.sale_price) AS spend
  FROM {ds}.orders AS o JOIN {ds}.order_items AS oi ON oi.order_id = o.order_id
  WHERE oi.status = 'Complete' AND oi.product_id BETWEEN {scope_lo} AND {scope_hi}
    AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00'
  GROUP BY o.user_id),
banded AS (
  SELECT u.age - MOD(u.age, 5) AS band, c.spend, c.user_id
  FROM cust AS c JOIN {ds}.users AS u ON u.id = c.user_id),
by_band AS (SELECT band, SUM(spend) AS spend, COUNT(*) AS customers FROM banded GROUP BY band),
r_total AS (SELECT band, spend, ROW_NUMBER() OVER (ORDER BY spend DESC, band ASC) AS rk FROM by_band),
r_per AS (SELECT band, spend / customers AS per_customer, customers, ROW_NUMBER() OVER (ORDER BY spend / customers DESC, band ASC) AS rk FROM by_band)
SELECT
  (SELECT CONCAT(CAST(band AS STRING), '-', CAST(band + 4 AS STRING)) FROM r_total WHERE rk = 1) AS top_total_band,
  ROUND((SELECT spend FROM r_total WHERE rk = 1), 2) AS top_total_band_spend,
  (SELECT CONCAT(CAST(band AS STRING), '-', CAST(band + 4 AS STRING)) FROM r_per WHERE rk = 1) AS top_per_customer_band,
  ROUND((SELECT per_customer FROM r_per WHERE rk = 1), 2) AS top_per_customer_amount,
  (SELECT customers FROM r_per WHERE rk = 1) AS top_per_customer_band_customers,
  ROUND((SELECT SUM(spend) FROM cust), 2) AS total_revenue,
  (SELECT COUNT(*) FROM {ds}.orders AS o JOIN {ds}.order_items AS oi ON oi.order_id = o.order_id
   WHERE oi.product_id BETWEEN {scope_lo} AND {scope_hi}
     AND o.created_at >= TIMESTAMP '{w_start} 00:00:00' AND o.created_at < TIMESTAMP '{w_end} 00:00:00') AS window_items_all_statuses;
