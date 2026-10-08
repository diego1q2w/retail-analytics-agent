-- Reference for scenario ho-l1-partial-month-comparable. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
-- As of 2026-10-13: only complete days (1..12 October) against 1..12 September.
WITH w AS (
  SELECT SUM(CASE WHEN o.created_at >= TIMESTAMP '2026-10-01 00:00:00' AND o.created_at < TIMESTAMP '2026-10-13 00:00:00' THEN oi.sale_price END) AS mtd,
         SUM(CASE WHEN o.created_at >= TIMESTAMP '2026-09-01 00:00:00' AND o.created_at < TIMESTAMP '2026-09-13 00:00:00' THEN oi.sale_price END) AS prior
  FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
  WHERE oi.status = 'Complete')
SELECT mtd AS month_to_date_revenue, prior AS comparable_prior_revenue,
       (mtd - prior) / prior * 100 AS change_percent
FROM w;
