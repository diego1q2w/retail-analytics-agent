-- Reference for scenario ho-l1-state-spend. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
WITH per_state AS (
  SELECT u.state, SUM(oi.sale_price) AS spend, COUNT(DISTINCT u.id) AS customers
  FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id JOIN thelook.users AS u ON u.id = o.user_id
  WHERE oi.status = 'Complete'
    AND o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-11-01 00:00:00'
  GROUP BY 1)
SELECT (SELECT state FROM per_state ORDER BY spend DESC LIMIT 1) AS top_state_total,
       (SELECT spend FROM per_state ORDER BY spend DESC LIMIT 1) AS top_state_total_amount,
       (SELECT state FROM per_state ORDER BY spend / customers DESC LIMIT 1) AS top_state_per_customer,
       (SELECT spend / customers FROM per_state ORDER BY spend / customers DESC LIMIT 1) AS top_state_per_customer_amount;
