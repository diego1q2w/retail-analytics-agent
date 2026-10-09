# rd-l1-age-band-spend-q4: Age groups by total and per-customer spend

## User turn 1

In the last three months of 2025, which age groups spent the most, in total and per customer?

## Assistant message 1

### Findings

During the last three months of 2025 (Q4: October 1, 2025, to December 31, 2025):

1. **Highest Total Spend**:
   - **Age 65–69** generated the highest total completed spend at **$9,035.13** (10.3% of total completed sales), across 93 purchasing customers [evd_d97a2c353a9b4b4891bc150205602dfa, evd_8782538c913841cdb1db16ead68043f3].
   - The next highest total spending groups were:
     - **35–39**: **$8,857.03** (94 purchasing customers) [evd_d97a2c353a9b4b4891bc150205602dfa]
     - **55–59**: **$8,782.96** (84 purchasing customers) [evd_d97a2c353a9b4b4891bc150205602dfa]
     - **50–54**: **$8,723.20** (100 purchasing customers) [evd_d97a2c353a9b4b4891bc150205602dfa]
     - **15–19**: **$8,441.71** (89 purchasing customers) [evd_d97a2c353a9b4b4891bc150205602dfa]

2. **Highest Spend Per Customer**:
   - **Age 55–59** had the highest average completed spend per customer at **$104.56** (84 purchasing customers, total spend $8,782.96) [evd_d97a2c353a9b4b4891bc150205602dfa].
   - **Age 65–69** ranked second with **$97.15** per customer (93 purchasing customers, total spend $9,035.13) [evd_d97a2c353a9b4b4891bc150205602dfa].
   - **Age 15–19** ranked third with **$94.85** per customer (89 purchasing customers, total spend $8,441.71) [evd_d97a2c353a9b4b4891bc150205602dfa].
   - **Age 35–39** ranked fourth with **$94.22** per customer (94 purchasing customers, total spend $8,857.03) [evd_d97a2c353a9b4b4891bc150205602dfa].

3. **Lowest Spending Group**:
   - **Age 70–74** spent the least both in total (**$1,088.61**) and per customer (**$64.04** across 17 customers) [evd_d97a2c353a9b4b4891bc150205602dfa].

---

### Complete Breakdown by Age Group (Q4 2025)

Ranked by **Total Completed Spend** [evd_d97a2c353a9b4b4891bc150205602dfa]:

| Age Band | Total Spend | Purchasing Customers | Orders | Items Sold | Spend per Customer |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **65–69** | **$9,035.13** | 93 | 94 | 151 | $97.15 |
| **35–39** | **$8,857.03** | 94 | 96 | 142 | $94.22 |
| **55–59** | **$8,782.96** | 84 | 86 | 132 | **$104.56** |
| **50–54** | **$8,723.20** | 100 | 102 | 135 | $87.23 |
| **15–19** | **$8,441.71** | 89 | 93 | 135 | $94.85 |
| **45–49** | **$7,348.20** | 94 | 94 | 139 | $78.17 |
| **40–44** | **$6,955.60** | 94 | 94 | 128 | $74.00 |
| **30–34** | **$6,260.66** | 85 | 87 | 121 | $73.65 |
| **25–29** | **$5,965.38** | 80 | 82 | 124 | $74.57 |
| **20–24** | **$5,825.64** | 79 | 80 | 119 | $73.74 |
| **60–64** | **$5,375.36** | 82 | 82 | 107 | $65.55 |
| **10–14** | **$4,702.82** | 57 | 57 | 82 | $82.51 |
| **70–74** | **$1,088.61** | 17 | 17 | 20 | $64.04 |

*Total completed sales across all permitted products in Q4 2025: $87,362.30 across 1,048 unique purchasing customers and 1,535 items [evd_8782538c913841cdb1db16ead68043f3].*

---

### Fulfillment Sensitivity Analysis

If considering all fulfilled orders (combining `Complete` and `Shipped` statuses) [evd_1aed105dc10e410a998c2c369749b6c8]:
- **65–69** remains the highest in total spend (**$20,346.64**) and highest per customer (**$96.89**).
- **55–59** remains second in total spend (**$17,981.05**) and spend per customer (**$94.64**).

---

### Definitions & Analytical Scope

- **Period**: Q4 2025 (October 1, 2025, to December 31, 2025, inclusive, UTC half-open window: `ordered_date >= '2025-10-01'` and `ordered_date < '2026-01-01'`).
- **Date Basis**: Order date (`ordered_date` UTC) on which the customer order was placed.
- **Metric Definitions**:
  - **Spend / Sales**: Completed item sales (`item_status = 'Complete'`).
  - **Purchasing Customers**: Count of distinct customer references (`customer_ref`) with completed sales within the period.
  - **Spend per Customer**: `total_spend` divided by `purchasing_customers`.
- **Scope**: Restricted to permitted retail products within your data permissions. Amounts are in the dataset's native currency units.

---

### Limitations

1. **Order Status Scope**: By standard definition, spend measures completed sales (`Complete`). Items in `Shipped` ($104,967.81 across 1,314 customers) or `Processing` ($72,694.51 across 834 customers) represent pending pipeline that had not yet reached completion [evd_8782538c913841cdb1db16ead68043f3].
2. **Sample Size Variations**: The **70–74** age bracket has a small customer sample (17 customers), which increases sensitivity to individual purchase variance [evd_d97a2c353a9b4b4891bc150205602dfa].
3. **Customer Demographics**: Ages are pre-binned into 5-year brackets; exact ages and sub-segment identities are opaque.

---

### Recommended Actions

1. **Prioritize 55–69 Campaigns**: The 55–59 and 65–69 cohorts represent the strongest spending power both in aggregate ($17,818.09 combined) and on a per-customer basis ($97–$105). Tailor marketing campaigns and high-ticket promotions toward these groups [evd_d97a2c353a9b4b4891bc150205602dfa].
2. **Engage High-Volume Mid-Age Groups (35–39 and 50–54)**: These groups show high customer participation (94 and 100 purchasing customers) and strong aggregate spend; evaluate basket-building strategies (cross-selling or bundling) to elevate their average spend closer to the 55–59 benchmark [evd_d97a2c353a9b4b4891bc150205602dfa].
3. **Investigate 60–64 and 70–74 Underperformance**: The 60–64 age band exhibits notably lower spend per customer ($65.55) compared to adjacent cohorts 55–59 ($104.56) and 65–69 ($97.15). Review product assortment appeal and conversion funnels for this demographic segment [evd_d97a2c353a9b4b4891bc150205602dfa].

_No report was saved in this conversation._
