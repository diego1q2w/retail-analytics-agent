-- Reference for scenario ho-l2-customer-concentration-opaque. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
WITH spend AS (
  SELECT o.user_id, SUM(oi.sale_price) AS amount
  FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id
  WHERE oi.status = 'Complete'
    AND o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-11-01 00:00:00'
  GROUP BY 1),
ranked AS (SELECT amount, ROW_NUMBER() OVER (ORDER BY amount DESC, user_id) AS rn FROM spend)
SELECT (SELECT amount FROM ranked WHERE rn = 1) AS top_customer_spend,
       (SELECT SUM(amount) FROM ranked WHERE rn <= 3) AS top_three_spend,
       (SELECT SUM(amount) FROM ranked WHERE rn <= 3) / (SELECT SUM(amount) FROM ranked) AS top_three_share;
