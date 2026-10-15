# DECISIONS.md

## 1. Indexes

I created indexes to support the main queries. The `emp_code` and `date` compound index supports attendance lookups by date and employee. The `emp_code` and `date` index supports an employee's monthly attendance query. An employee index on joining date and department helps the department summary. These indexes aim to reduce unnecessary document scans. I would avoid adding indexes without a query or constraint that justifies their write and storage costs.

## 2. Punch-in race

Two identical punch-in requests can arrive at the same time. Both may check whether a record already exists before either inserts one. The implementation must rely on an atomic database guarantee, such as a suitable unique index or atomic operation, to prevent duplicate records. If it relies only on checking first and inserting afterward, a race can create duplicates. I would verify this with concurrent requests.

## 3. Ties

The leaderboard sorts employees by late minutes and applies the requested limit. If employees tie at the cutoff, the result depends on the secondary sort and limit implementation; tied employees may not all be included.

## 4. Headcount

The department summary starts from employee records and looks up attendance logs. Therefore, employees without attendance logs can still be counted in their department.

## 5. Scaling

For 100 times more data, I would run concurrent-load tests, inspect query plans, and measure response times before tuning indexes and aggregation pipelines.