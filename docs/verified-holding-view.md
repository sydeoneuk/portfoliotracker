# Verified holding views across broker aliases

The legacy storage model keys financial records by broker ticker and account.
Those references must remain intact, but an exact-ticker holding page hid other
accounts for the same security. The holding reader now resolves an owner's
verified security identity and reads all matching account/ticker scopes.

The resolver accepts current account-scoped or global listing links and checked
personal mapping evidence. An owner's independently verified provider reference
can also validate an alias declaring that exact ISIN and provider symbol, with
compatible quote units and venue. Catalogue ISINs and symbol/name hints alone do
not establish equivalence. Stale or deliberately removed scoped links, stale
global links and retained identity/unit conflicts block expansion. Discovery and
every financial query remain owner-scoped. Sold aliases can contribute activity
and recorded history even after their current position disappears.

Either linked alias URL shows the same combined position, trades and received
dividends. Account filters apply to every query; an empty or foreign account no
longer falls back to all positions. Each quote is valued in its own explicit
currency/units before conversion and summation. Purchase costs use their original
recorded currencies. Per-share forecasts are converted, selected rather than
summed across aliases, and remain separate from actual received cash events.
Dividend per-share display uses the source alias's units. Per-share historical
market datasets otherwise remain attached to the requested alias.

Account badges and original broker references are visible in the holding view.
Potential same-ISIN references lacking verification link to the existing Settings
matching workflow instead of being silently combined. Historical charts read
preserved GBP snapshot values; no native historical amounts are reinterpreted.

No migration, backfill, financial-row merge, event deduplication, import or provider
call occurs when opening the page (existing display FX lookups are unchanged).
The change is read-only. Raw aliases remain storage/source references; the broader
account/provider/connection separation and canonical-event refactor are still
separate work. Other portfolio/analysis list projections still retain their
original broker-ticker rows; they link into this shared holding view.

Validation: 348 Python tests, 43 subtests and 26 Node UI tests passed in WSL Docker.
Tests cover alias URLs, account filtering, GBP/GBX and USD conversion, sold
accounts, identical external event IDs across accounts, unverified/other-ISIN
exclusion, stale/removed links, retained execution-unit conflicts and owner scope.
A read-only local-data check verified both SMT/SMTl_EQ and TRIG/TRIGl_EQ show two
accounts, equivalent combined totals and reconciling per-account values. SMT has
10 trades and 3 dividend payments; TRIG has 8 trades and 5 payments. Fingerprints
of eight original financial/source tables remained unchanged. Production untouched.
