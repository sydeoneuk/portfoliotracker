-- ============================================================
-- One-time cleanup: remove rows with no user_id association
-- Run ONCE after deploying migrations 014 and 015.
-- Safe to run multiple times (no rows will match after first run).
-- ============================================================

BEGIN;

-- Transactions with no owner
DELETE FROM transactions
WHERE user_id IS NULL;

-- Orders with no owner (open orders and historical)
DELETE FROM orders
WHERE user_id IS NULL;

-- Positions with no owner
DELETE FROM positions
WHERE user_id IS NULL;

-- Dividend payments with no owner
DELETE FROM dividend_payments
WHERE user_id IS NULL;

-- Pies (and their holdings via CASCADE) with no owner
-- Migration 015 already deleted all pre-existing pie data,
-- so this is a safety net for any rows inserted before the migration.
DELETE FROM pie_holdings
WHERE user_id IS NULL;

DELETE FROM pies
WHERE user_id IS NULL;

COMMIT;

-- Verify nothing remains
SELECT 'transactions'    AS tbl, COUNT(*) AS orphan_count FROM transactions    WHERE user_id IS NULL
UNION ALL
SELECT 'orders',                  COUNT(*)                FROM orders            WHERE user_id IS NULL
UNION ALL
SELECT 'positions',               COUNT(*)                FROM positions         WHERE user_id IS NULL
UNION ALL
SELECT 'dividend_payments',       COUNT(*)                FROM dividend_payments WHERE user_id IS NULL
UNION ALL
SELECT 'pies',                    COUNT(*)                FROM pies              WHERE user_id IS NULL
UNION ALL
SELECT 'pie_holdings',            COUNT(*)                FROM pie_holdings      WHERE user_id IS NULL;
