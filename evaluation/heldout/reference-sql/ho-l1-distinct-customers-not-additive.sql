-- Reference for scenario ho-l1-distinct-customers-not-additive. Raw source tables, UTC, half-open windows.
-- Output: one row; column names are the manifest observation names.
-- Scope: all products. Customers are counted once per brand set, not summed.
SELECT COUNT(DISTINCT CASE WHEN p.brand = 'Aster' THEN o.user_id END) AS aster_customers,
       COUNT(DISTINCT CASE WHEN p.brand = 'Birch' THEN o.user_id END) AS birch_customers,
       COUNT(DISTINCT CASE WHEN p.brand IN ('Aster', 'Birch') THEN o.user_id END) AS either_customers,
       (SELECT COUNT(*) FROM (
          SELECT o2.user_id
          FROM thelook.order_items AS i2
          JOIN thelook.orders AS o2 ON o2.order_id = i2.order_id
          JOIN thelook.products AS p2 ON p2.id = i2.product_id
          WHERE i2.status = 'Complete'
            AND o2.created_at >= TIMESTAMP '2026-08-01 00:00:00'
            AND o2.created_at < TIMESTAMP '2026-11-01 00:00:00'
            AND p2.brand IN ('Aster', 'Birch')
          GROUP BY o2.user_id
          HAVING COUNT(DISTINCT p2.brand) = 2)) AS both_customers
FROM thelook.order_items AS oi JOIN thelook.orders AS o ON o.order_id = oi.order_id JOIN thelook.products AS p ON p.id = oi.product_id
WHERE oi.status = 'Complete'
  AND o.created_at >= TIMESTAMP '2026-08-01 00:00:00' AND o.created_at < TIMESTAMP '2026-11-01 00:00:00';
