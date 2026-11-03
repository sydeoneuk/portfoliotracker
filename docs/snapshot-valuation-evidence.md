# Snapshot valuation evidence (047)

New holding snapshots freeze `price_currency`, `price_unit_scale`,
`fx_rate_to_gbp` and `valuation_evidence`. Both API and CSV writers use the same
evidence builder. The currency and scale describe `price_native`; verified quote
evidence separately retains the provider's original price/units, listing identity,
resolution revision, statement authority, observation/fetch times and conversion.
Legacy effective-price selection is explicitly labelled with unknown provider
identity rather than claiming independent listing verification.

The recorded GBP FX rate is the actual rate used by this valuation, including
pence conversion. It does not claim an FX provider observation timestamp that the
existing FX interface does not supply. The calculation version is `snapshot-1`.

Missing catalogue units are supported only when the existing guarded account
quote supplies explicit normalized units. Unpriced or invalid accounts still
fail before replacing their daily history; costs are never used as prices.

Historical reads use frozen snapshot currency rather than today's catalogue.
For pre-047 rows with unknown units, display uses the preserved GBP value and a
per-unit GBP equivalent derived from that total. This does not assign a currency
to the original `price_native` or update it. Existing historical GBP totals remain
authoritative; this migration does not recalculate history.

Migration 047 adds four nullable columns, without backfill or financial updates.
Keep these columns when rolling application code back; destructive downgrade is
intentionally disabled. Deploy the migration before enabling new writers/readers.
Previously recorded listing/currency/FX semantics cannot be recovered from current
metadata alone. Historical cost reconciliation remains separate.

Validation: empty-database upgrade and WSL Docker PostgreSQL tests passed:
316 Python tests, 43 subtests and 24 Node UI tests. A fresh local backup was
verified by restoration; rehearsal preserved all original values across 37,753
rows in 36 tables. Tests cover frozen units after catalogue edits, explicit GBP
quotes with missing catalogue units, preserved legacy amounts, and existing
API/CSV valuation, missing-FX and atomic replacement behaviour. Production has
not been accessed or migrated.
