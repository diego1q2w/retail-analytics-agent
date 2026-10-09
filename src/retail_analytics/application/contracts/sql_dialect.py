"""What the restricted query compiler accepts, worded for the model.

One source for the tool schema description (``capabilities.analysis``), the
investigation policy and the compiler's correction feedback, so they never
disagree. The compiler's grammar is the authority; these texts only describe
it. ``tests/unit/sql_compiler`` compiles and runs :data:`LATEST_MONTH_EXAMPLE`
so the documented pattern stays valid.
"""

from __future__ import annotations

# Joins: both sides must be approved relations, linked on a declared join.
SQL_JOIN_RULE = (
    "JOIN only approved relations to each other, on a declared join "
    "(one equality on the declared fields; INNER or LEFT; each relation "
    "once per query level). Never JOIN a CTE or subquery, even a one-row "
    "one: to filter by a computed value (latest date, latest year), compare "
    "with a scalar subquery in WHERE instead."
)

# Most recent occurrence of a month and its revenue, without a derived join.
LATEST_MONTH_EXAMPLE = (
    "SELECT MIN(s.ordered_date) AS first_day, MAX(s.ordered_date) AS last_day, "
    "SUM(s.sale_amount) AS revenue FROM sales_items AS s "
    "WHERE s.item_status = 'Complete' "
    "AND EXTRACT(MONTH FROM s.ordered_date) = @month "
    "AND EXTRACT(YEAR FROM s.ordered_date) = "
    "(SELECT MAX(EXTRACT(YEAR FROM x.ordered_date)) FROM sales_items AS x "
    "WHERE EXTRACT(MONTH FROM x.ordered_date) = @month)"
)

SQL_DIALECT_NOTE = (
    "Allowed: WITH (CTEs; the main query may select FROM one CTE), JOIN, "
    "WHERE, GROUP BY, HAVING, ORDER BY, LIMIT, DISTINCT, uncorrelated scalar "
    "subqueries (in SELECT or WHERE), CASE/IF, + - *, SUM, AVG, MIN, MAX, "
    "COUNT, COUNTIF, COALESCE, NULLIF, ABS, ROUND, LOWER, UPPER, DATE_TRUNC, "
    "EXTRACT, DATE_ADD, DATE_SUB, DATE_DIFF, CAST, IN, BETWEEN, LIKE, EXISTS. "
    f"{SQL_JOIN_RULE} Not allowed: the / operator (use SAFE_DIVIDE(a, b)), "
    "window functions (OVER; use ORDER BY ... LIMIT in a CTE or a scalar "
    "subquery), correlated subqueries, SELECT * (COUNT(*) is fine). Give "
    "tables aliases and qualify columns (s.product_id). Pass literal values "
    "as named @parameters or DATE 'YYYY-MM-DD' literals. Example, revenue "
    "of the latest September in the data (parameters {month: 9}): "
    f"{LATEST_MONTH_EXAMPLE}"
)

# Correction feedback for a rejected derived join (returned to the model).
DERIVED_JOIN_FEEDBACK = (
    "Joins to CTEs or subqueries are not supported; join only approved "
    "relations and filter by a computed value with a scalar subquery in WHERE"
)
