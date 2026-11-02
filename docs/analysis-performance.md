# Analysis valuation query performance

The local Analysis request regressed to 44.567 seconds. cProfile and SQL timing
attributed 43.734 seconds to database execution: the page ran the same 81,309-character
portfolio query twice, each taking roughly 21–23 seconds. Repeated strict sums,
PnL fields, price columns and ordering expanded the guarded account/legacy price
selector many times.

Current portfolio queries now materialize one `valuation_price` per owned position
in a request-scoped PostgreSQL CTE. Aggregates reuse that field. The guarded price
expression itself is unchanged, including account/source ownership, selected source,
revision/metadata validity, quote expiry and unavailable-value behaviour. Existing
pie CTEs compose with the priced-position CTE. This is query-local materialization,
not a persisted cache, backfill or migration.

EXPLAIN ANALYZE showed that even the smaller query spent 2,184.81 ms compiling 369
JIT functions; execution without JIT took 18.441 ms. `execute_position_query` disables
JIT only while executing these portfolio projections and restores the previous
transaction setting after success. SQL rollback resets a failed transaction's local
setting. No global database configuration is changed.

Whole-portfolio Analysis retains its initial, unnormalised rows before country/sector
filtering and display conversion, and reuses them for the full portfolio total.
Account/pie-filtered requests still fetch the full portfolio where required. No
values are reused across requests, and prices in pence cannot be converted twice.

The same local request subsequently rendered in 0.823 seconds, including 0.05
seconds of database time; its valuation query took 0.035 seconds. Measurement used
the largest local owner's actual data, read-only, including ordinary FX fetching.
Browser/network latency and a slower external FX provider can add to these times.

Validation: 300 Python tests and 37 subtests passed in WSL Docker. Regressions
compare optimized/unoptimized combined, single account, account type, multiple
accounts, pie and outside-pie queries; check missing quotes, per-account prices and
prior JIT setting restoration; and verify one whole-portfolio fetch plus unfiltered
native-pence totals when country filtering. A read-only comparison on actual local
data matched 1,302 rows across six query shapes (all monetary values, coverage and
filters, allowing only floating-point tolerance of 1e-8). No financial data or
historical records were rewritten. Production was not accessed or deployed.
