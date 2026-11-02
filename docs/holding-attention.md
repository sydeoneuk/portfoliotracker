# Customer review for unpriced holdings

Settings → **Holdings needing attention** lists current positive holdings belonging
to the signed-in owner in active accounts. Search and pagination keep reviews
bounded. Portfolio/analysis warnings and import matching results link to this area.
The older personal mapping tools remain available under a collapsed advanced panel.

The app now [attempts automatic ISIN verification](automatic-isin-matching.md)
before showing manual review forms. The following flow applies only to unresolved
exceptions:

1. Confirm the ISIN and quote units against a dated statement or reference.
2. Verify investment: search provider listings, or inspect an entered symbol.
3. If several listings verify, choose one and verify that choice.
4. Review the listing, exchange, units, price and observation time; choose
   **Confirm match and refresh price**.

Retained source claims can prefill the form. Shared catalogue fields are labelled
hints and require an explicit copy action and statement confirmation. A familiar
symbol or investment name never establishes identity. Source contradictions block
review rather than allowing an override. Invalid/unverified candidates cannot
produce Apply; editing fields invalidates the preview. Stale reviews send the user
back to verification rather than leaving an Apply loop.

The server derives the holdings scope from the account's selected source; clients
cannot choose an arbitrary namespace or another owner's holding. Verify/apply use
the existing actor-bound, expiring signed tokens, source fingerprint guards, account
locks and audit receipts. Refresh runs after the match commit, so provider failure
cannot undo it. Removed matches are not recreated by replaying an old token.

## Audit and defaults

A read-only audit on 2 November 2026 found 158 unpriced local account holdings:
all were legacy holdings without retained source ISIN or quote-unit evidence.
All had catalogue ISIN hints; 52 also lacked catalogue currency. No conflicting
retained source claims were found. These are account-holding counts, not distinct
securities. At that initial audit, no live corrections had been applied. The later
[automatic ISIN implementation](automatic-isin-matching.md) verified and priced
four local AAIF account holdings without changing their original financial data.

For missing catalogue units, a newly verified market quote is explicitly converted
to GBP as a display denomination. This does not fill shared metadata, infer the
units of an old broker price, or assign currency to legacy costs/dividend rates.
Unverified legacy prices with unknown units remain unavailable. Current portfolio
and holding valuation use this market display contract. Snapshot reads and writes
retain their existing currency requirements: accounts with unknown catalogue units
cannot yet write complete daily snapshots, even when their current market quote
can be displayed. Persisting explicit snapshot units is separate follow-up work.
Metrics with ambiguous legacy units are suppressed in current position displays.
Explicitly denominated cost components are retained.

## Validation

No migration or backfill is required beyond the existing revision 046. The full
Python suite passed in WSL Docker (276 tests, 31 subtests). After the final
source-contradiction test and snapshot safety check, all 21 targeted tests passed.
All 22 JavaScript flow tests passed, including explicit statement confirmation,
choice/re-verification, Apply visibility, stale reviews and price-refresh failure.
Tests preserve original financial records and verify ordinary owner access,
cross-origin rejection, account isolation, unknown-unit GBP display, source changes
and retry after undo. Read-only checks on actual local data found 158 owner-scoped
attention records, no automatically created resolutions, and unchanged legacy
price-selection coverage (643 priced, 158 unavailable). Production was untouched.
