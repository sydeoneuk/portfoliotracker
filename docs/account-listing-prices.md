# Verified account prices (migration 046)

Verified holdings matches now supply prices for that account. Unmatched holdings
keep the previous price-selection rules. An account match takes priority over
personal ticker mappings and global quotes; a missing or invalid account quote
does not fall back to a potentially different legacy listing.

## Quote contract

`account_listing_prices` is a derived cache keyed by resolution ID. It retains the
provider's original price, currency, unit scale, observation time and fetch time,
plus an explicit conversion rate and price in the catalogue's existing display
units. GBP/GBX conversion happens once. Other currencies require explicit FX;
missing FX makes the quote unavailable. With missing catalogue units, the subsequent
[customer attention workflow](holding-attention.md) explicitly displays a newly
verified quote in GBP without assigning units to legacy amounts. Cost fields, transactions,
instrument metadata and historical snapshots are not rewritten.

The provider must still verify the exact ISIN, exchange, symbol, currency and units
of the selected listing. Quotes must be at most seven days old and not future-dated
(five-minute clock tolerance). Fetches expire after 24 hours or when the observation
becomes seven days old, whichever is earlier. A transient refresh failure preserves
a previously verified quote only until its existing expiry, and records the error.

Readers check resolution revision/fingerprint, owner, selected statement batch,
catalogue metadata, units and expiry. A new statement invalidates the old cache
until refresh, even when the verified identity can be reused. Quote refreshes use
the account lock and recheck source evidence after provider calls. Undoing/removing
a resolution removes its derived price through a cascading FK.

Only current **holdings** scopes price positions. Event and pie matches cannot be
silently promoted to holdings evidence. Scheduled price refreshes fetch current,
positive holdings for active accounts. Matched imports attempt a separate refresh
after successful import/matching; provider failure cannot roll back either result.
Settings → Import history and source review → **Match investments and refresh prices**
automatically verifies existing ISINs and refreshes only the signed-in owner's
matches; cross-origin writes are rejected. See `automatic-isin-matching.md`.

Dashboard, analysis, holding details and future snapshot writes use account prices.
Combined price displays are quantity-weighted across accounts; they never choose
one account's quote for every account. Historical snapshot reads remain unchanged.
Unavailable holdings show refresh/match/FX reasons where available.

## Validation and rollout

Migration 046 adds one empty cache table. No backfill or legacy-row update occurs.
Retain the schema when rolling application code back; downgrade intentionally
refuses to drop evidence.

The WSL Docker Python suite passed (267 tests, 31 subtests). After adding the final
refresh endpoint check and restricting refresh to currently held scopes, the 17
pricing/matching integration tests passed. All 17 JavaScript flow tests passed.
Tests cover account isolation, pence and FX conversion, unavailable/expired quotes,
source changes, removed matches, weighted displays, holding detail and ownership.

A local dump was verified by restoring it into an isolated database at revision
045. The 045→046 upgrade preserved fingerprints of all 35,630 original rows across
35 tables. The read-only comparison scanned 801 positive account holdings in 0.21
seconds: 643 remained priced, 158 remained unavailable, and none changed price or
availability. This baseline has no applicable verified account prices; fixture
tests establish the new pricing behaviour. Production was not accessed or deployed.

Run `python scripts/compare_account_prices.py [--user-id ID]` for a read-only,
network-free comparison. Differences can reflect changed listings or quote times
and are not automatically errors. The output reports coverage and change counts
without summing amounts across currencies.
