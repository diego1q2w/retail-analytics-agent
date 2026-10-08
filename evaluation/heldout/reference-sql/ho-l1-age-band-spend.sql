-- Reference for scenario ho-l1-age-band-spend. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
-- 5-year bands from age; spend per distinct customer within each band.
WITH per_band AS (
  SELECT (u.age // 5) * 5 AS band_start, SUM(oi.sale_price) AS spend, COUNT(DISTINCT u.id) AS customers
  FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id JOIN thelook.users AS u ON u.id = o.user_id
  WHERE oi.status = 'Complete'
    AND o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-11-01 00:00:00'
  GROUP BY 1)
SELECT MAX(CASE WHEN band_start = 20 THEN spend / customers END) AS spend_per_customer_20_24,
       MAX(CASE WHEN band_start = 25 THEN spend / customers END) AS spend_per_customer_25_29,
       MAX(CASE WHEN band_start = 30 THEN spend / customers END) AS spend_per_customer_30_34,
       MAX(CASE WHEN band_start = 35 THEN spend / customers END) AS spend_per_customer_35_39
FROM per_band;
