# rd-l2-customer-concentration-report: Customer concentration summary

## User turn 1

How concentrated is our customer spend in the fourth quarter of 2025? What share of revenue comes from our ten biggest customers?

## User turn 2

Put that in a short summary with what we should keep an eye on.

## Assistant message 1

### Key Findings

1. **Share of Revenue from the Top 10 Customers**
   - In Q4 2025 (October 1 to December 31, 2025), total completed sales across permitted products were **88,611.08** across **1,004** unique purchasing customers [evd_d3fafc60a44e43e584f9429fa0d58b6e].
   - The top 10 customers generated a combined spend of **5,756.37**, representing **6.50%** of total revenue [evd_d9f16ec7a2a944a894cd5fc4b7929ef6].
   - The single largest customer (`cus_542e48cb2c2c0138238a0acf`) accounted for **903.00**, or **1.02%** of total revenue [evd_cd10b0ceecd04410a710c32c41e55f14].

2. **Overall Spend Concentration**
   - **Low Top-End Concentration Risk**: Because the top 10 customers (~1.00% of customer count) represent only 6.50% of sales [evd_d9f16ec7a2a944a894cd5fc4b7929ef6, evd_d3fafc60a44e43e584f9429fa0d58b6e], and the top 100 customers (~9.96% of customer count) account for **29,967.42** or **33.82%** of revenue [evd_2ccf6faa3ca443a3996c2b7432c1d49f], the business is not overly dependent on a small handful of buyers.
   - **Broad Core Segment**: Customers spending between **100 and 499.99** drive the core of business revenue, accounting for **59.19%** of sales (52,453.21) across 281 customers (27.99% of total customers) [evd_3e231adcca7b431fac6f8e70df8c9101].
   - **Long Tail of Smaller Buyers**: Customers spending under 100 make up **71.31%** of all purchasers (716 customers), generating **35.89%** of total revenue (31,806.06) [evd_3e231adcca7b431fac6f8e70df8c9101]. Only 7 customers (0.70%) spent 500 or more [evd_3e231adcca7b431fac6f8e70df8c9101].

---

### Top 10 Customers Detail (Q4 2025)

All top 10 customers completed 1 order during the quarter [evd_cd10b0ceecd04410a710c32c41e55f14]:

| Customer Reference | Orders | Items Purchased | Total Spend | Share of Total Revenue |
| :--- | :---: | :---: | :---: | :---: |
| `cus_542e48cb2c2c0138238a0acf` | 1 | 1 | 903.00 | 1.02% |
| `cus_d2351da54f50dee0647c0798` | 1 | 1 | 758.00 | 0.86% |
| `cus_626fc14b99c8ccf550d55dc2` | 1 | 1 | 598.00 | 0.67% |
| `cus_f34e1b19dd6f6243c277c5ec` | 1 | 1 | 550.00 | 0.62% |
| `cus_ec23c9bc530d5e3f733b37f3` | 1 | 1 | 525.00 | 0.59% |
| `cus_b3b03d7826a85dff435e5542` | 1 | 2 | 515.88 | 0.58% |
| `cus_0344f63fe9087d61e063875c` | 1 | 4 | 501.93 | 0.57% |
| `cus_ea546e2d8013e7e1f7d27c7a` | 1 | 4 | 483.75 | 0.55% |
| `cus_02ff20aaef5a039916f9a83d` | 1 | 2 | 477.00 | 0.54% |
| `cus_99ce5e7b8f142bd15582eb19` | 1 | 4 | 443.81 | 0.50% |
| **Top 10 Total** | **10** | **22** | **5,756.37** | **6.50%** |

*(Source: [evd_cd10b0ceecd04410a710c32c41e55f14], [evd_d9f16ec7a2a944a894cd5fc4b7929ef6])*

---

### Customer Spend Distribution Across Tiers

| Spend Tier | Customer Count | % of Customers | Total Tier Revenue | % of Total Revenue | Avg Spend per Customer |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Under 50 | 449 | 44.72% | 12,985.77 | 14.65% | 28.92 |
| 50 to 99.99 | 267 | 26.59% | 18,820.29 | 21.24% | 70.49 |
| 100 to 199.99 | 195 | 19.42% | 28,234.06 | 31.86% | 144.79 |
| 200 to 499.99 | 86 | 8.57% | 24,219.15 | 27.33% | 281.62 |
| 500 and above | 7 | 0.70% | 4,351.81 | 4.91% | 621.69 |
| **Total** | **1,004** | **100.00%** | **88,611.08** | **100.00%** | **88.26** |

*(Source: [evd_3e231adcca7b431fac6f8e70df8c9101], [evd_d3fafc60a44e43e584f9429fa0d58b6e])*

---

### Scope and Definitions
- **Period**: Fourth quarter of 2025 (`2025-10-01` to `2025-12-31` inclusive, based on UTC order date `ordered_date`).
- **Scope**: Filtered strictly to orders and items within your permitted product catalogue.
- **Metric Definitions**:
  - Revenue is defined as completed item sales (`item_status = 'Complete'`).
  - Customer counts reflect distinct customer references with completed orders in the period.
- **Currency**: All amounts are reported in the source dataset currency (unconverted).

---

### Limitations
- **Quarterly Horizon**: This analysis covers a single quarter and reflects quarterly purchasing behaviour; it does not measure long-term customer lifetime value (LTV) or annual retention.
- **Single-Order Tendency**: All top 10 spenders in Q4 placed exactly 1 order during the quarter [evd_cd10b0ceecd04410a710c32c41e55f14]. High quarterly spend among these accounts was driven by order basket size/value rather than repeated transactions within the quarter.

---

### Suggested Actions
1. **Engage Top Spenders for Repeat Orders**: Because the top 10 customers each placed only one order in Q4 [evd_cd10b0ceecd04410a710c32c41e55f14], evaluate post-purchase onboarding or targeted replenishment communications to convert high-basket single buyers into repeat customers.
2. **Basket Building for the Under-100 Segment**: With 71.31% of purchasers spending under 100 [evd_3e231adcca7b431fac6f8e70df8c9101], test bundle discounts, tiered shipping thresholds, or cross-sell recommendations to lift average basket sizes into the 100+ tier.

## Assistant message 2

### Executive Summary

In Q4 2025 (October 1 to December 31, 2025), customer spend concentration was **very low and healthy**:
- **Total Revenue**: 88,611.08 across 1,004 purchasing customers and 1,017 completed orders [evd_d3fafc60a44e43e584f9429fa0d58b6e].
- **Top 10 Customers**: Accounted for **6.50%** of total revenue (5,756.37) [evd_d9f16ec7a2a944a894cd5fc4b7929ef6], with the single largest customer contributing only **1.02%** (903.00) [evd_cd10b0ceecd04410a710c32c41e55f14].
- **Top 100 Customers**: Represented roughly 10% of buyers and generated **33.82%** of revenue (29,967.42) [evd_2ccf6faa3ca443a3996c2b7432c1d49f].
- **Revenue Engine**: The business is primarily powered by the mid-tier (spend between 100 and 499.99), which drove **59.19%** of sales (52,453.21 across 281 customers) [evd_3e231adcca7b431fac6f8e70df8c9101].

---

### What to Keep an Eye On

1. **Near-Zero Repeat Purchasing Within the Quarter**
   - **Observation**: 1,004 customers placed 1,017 orders (~1.01 orders per customer) [evd_d3fafc60a44e43e584f9429fa0d58b6e], and all top 10 customers placed exactly 1 order in Q4 [evd_cd10b0ceecd04410a710c32c41e55f14].
   - **Watchout**: Top spend is driven by one-time basket size rather than repeat engagement. Monitor quarterly repeat purchase rates and retention cohorts to ensure the business is not entirely dependent on continuous new customer acquisition.

2. **Heavy Tail of Sub-100 Shoppers**
   - **Observation**: 71.31% of customers (716 buyers) spent under 100, contributing 35.89% of revenue (31,806.06) [evd_3e231adcca7b431fac6f8e70df8c9101].
   - **Watchout**: While broad distribution mitigates client churn risk, low average order values (28.92 for the sub-50 tier) [evd_3e231adcca7b431fac6f8e70df8c9101] leave net margins vulnerable to shifts in fulfillment and acquisition costs. Track whether minimum order thresholds, bundles, or cross-sells can migrate buyers from <100 into the 100–199 bracket.

3. **Very Small High-Roller Segment**
   - **Observation**: Only 7 customers (0.70%) spent 500 or more (totaling 4,351.81 or 4.91% of revenue) [evd_3e231adcca7b431fac6f8e70df8c9101].
   - **Watchout**: There is substantial headroom to grow premium tiers through VIP loyalty incentives, high-ticket product discovery, or post-purchase follow-ups.

---

### Definitions & Limitations
- **Scope & Definitions**: Permitted products only. Revenue is measured as completed item sales (`status = 'Complete'`). Order dates are evaluated on UTC order creation time (`orders.created_at`). Amounts are reported in the native dataset currency.
- **Period**: Q4 2025 (2025-10-01 00:00:00 to 2025-12-31 23:59:59 UTC).
- **Limitations**: Analysis reflects a single isolated quarter; customer purchases occurring before October 1, 2025, or after December 31, 2025, are outside this observation window.

_No report was saved in this conversation._
