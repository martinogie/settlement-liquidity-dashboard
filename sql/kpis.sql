-- Settlement and liquidity KPIs in SQL (SQLite). Each query is named with "-- name:".
-- Tables: batches, bank_lines, links, recon, balances (built by `python -m settlement.run`).

-- name: match_rate_by_rule
-- How each expected batch was matched; manual = matched by operations after the engine.
SELECT COALESCE(r.rule, CASE WHEN r.manual_date <= :as_of THEN 'manual' ELSE 'open' END) AS rule,
       COUNT(*)                              AS batches,
       ROUND(SUM(r.amount) / 1e6, 1)          AS amount_musd,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM recon WHERE expected_date <= :as_of), 2) AS pct
FROM recon r
WHERE r.expected_date <= :as_of
GROUP BY 1
ORDER BY batches DESC;

-- name: on_time_by_source_month
-- Share of expected settlement value received on or before its expected date.
SELECT source,
       substr(expected_date, 1, 7) AS month,
       ROUND(100.0 * SUM(CASE WHEN matched = 1 AND received_date <= expected_date THEN amount ELSE 0 END)
             / SUM(amount), 1) AS on_time_pct,
       ROUND(AVG(CASE WHEN received_date IS NOT NULL
                      THEN julianday(received_date) - julianday(expected_date) END), 2) AS avg_days_late
FROM recon
WHERE expected_date <= :as_of
GROUP BY source, month
ORDER BY source, month;

-- name: open_exceptions_aging
-- Expected batches not yet received (or not yet matched by hand) as of a date, aged in calendar days.
SELECT source,
       ROUND(SUM(CASE WHEN age <= 1 THEN amount ELSE 0 END)) AS d0_1,
       ROUND(SUM(CASE WHEN age BETWEEN 2 AND 3 THEN amount ELSE 0 END)) AS d2_3,
       ROUND(SUM(CASE WHEN age BETWEEN 4 AND 7 THEN amount ELSE 0 END)) AS d4_7,
       ROUND(SUM(CASE WHEN age > 7 THEN amount ELSE 0 END)) AS d8_plus,
       ROUND(SUM(amount)) AS total
FROM (
  SELECT source, amount, julianday(:as_of) - julianday(expected_date) AS age
  FROM recon
  WHERE expected_date <= :as_of
    AND ((matched = 1 AND received_date > :as_of)
      OR (matched = 0 AND (manual_date IS NULL OR manual_date > :as_of)))
)
GROUP BY source
ORDER BY total DESC;

-- name: liquidity_coverage
-- Available cash (total less restricted reserve) in days of average payouts over the last 20 business days.
SELECT date,
       ROUND(total_cash / 1e6, 1) AS total_musd,
       ROUND(available / 1e6, 1)  AS available_musd,
       ROUND(available / AVG(payouts_today) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 2)
         AS coverage_days
FROM balances
ORDER BY date DESC
LIMIT 10;

-- name: bank_concentration
-- Largest single-bank share of cash by month (average of daily shares).
SELECT substr(date, 1, 7) AS month,
       ROUND(100 * AVG(MAX(bank_a, bank_b, bank_c) / total_cash), 1) AS top_bank_pct,
       ROUND(100 * MAX(MAX(bank_a, bank_b, bank_c) / total_cash), 1) AS worst_day_pct
FROM balances
GROUP BY month
ORDER BY month;
