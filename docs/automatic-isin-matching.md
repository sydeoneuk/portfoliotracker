# Automatic ISIN matching

Existing ISINs now initiate provider verification for legacy and imported holdings.
Missing retained statement fields do not, by themselves, require customer input.
Retained source identity takes precedence; otherwise the catalogue ISIN is used as
a lookup key. The provider must independently return the exact same ISIN, a usable
recent quote, explicit currency/units, and a recognised exchange MIC.

Catalogue aliases with the same ISIN contribute symbol and venue preferences.
They are not accepted as identity proof. In particular, legacy `AAIF` can discover
`AAIF.L` via the `AAIFl_EQ` catalogue entry for ISIN `GB00B0P6J834`, then validate
that public listing before creating an account-specific resolution. An invalid
bare-symbol hint does not invalidate another successfully verified candidate.

Retained MIC and quote-unit claims remain hard constraints. Catalogue-only venue
and currency are preferences among independently verified candidates, not barriers
to discovering a unique valid listing. Missing quote units are taken from the
verified provider response. Several remaining verified candidates require review.
Unknown ISIN, contradictory source evidence, provider failure, stale existing
resolutions and deliberately removed matches also remain review cases.

New matches use existing signed-token/source guards, account locks and audit
receipts. The claim reference explicitly records **Automatic provider validation**;
no statement evidence is invented. Only derived account matches and quote caches
are written. Costs, source files, transactions, shared metadata and historic values
remain unchanged. No migration is required. Missing legacy currency can still use
the existing verified-market-quote GBP display contract.

Automatic repair runs:

- after reviewed CSV imports;
- before scheduled quote refreshes;
- from Settings → **Match investments and refresh prices**;
- before manual forms are shown when loading a page of holdings needing attention.

Browser actions process one holding per request and display progress, so a whole
portfolio does not depend on a single long request. Successfully committed items
survive interruption and are reused on retry. Scheduled refreshes share a short-lived
provider lookup cache across accounts; account decisions remain independently scoped.
GET list/work endpoints are read-only; matching and price writes require POST,
authentication, owner checks and same-origin protection.

Validation: the full WSL Docker Python suite passed (286 tests, 31 subtests), as did
all 23 JavaScript flow tests. Regressions cover the exact AAIF legacy pattern with a
stubbed provider, missing units, bad symbol hints, exact ISIN verification,
ambiguity, source precedence, owner isolation, removed matches and automatic
refresh. The AAIF fixture preserves original financial records and restores pricing.
After explicit user clarification that provider requests may send public ISINs and
symbols, live verification succeeded: Yahoo supplied `AAIF.L`, ISIN `GB00B0P6J834`,
GBX and XLON; OpenFIGI confirmed the ISIN/venue/ticker association. No account IDs,
user identity, quantities or costs were included in the provider requests.

A fresh local backup was verified by restoration. The automatic AAIF correction
then succeeded on the restored copy and on local Docker: four account holdings
were matched and priced. Fingerprints of all 35,631 pre-existing rows across the
31 tables outside the derived identity/quote tables remained unchanged. Only the
derived security/listing/provider identities, account resolutions, audit receipts
and quotes were added. Standalone workers also register the users ORM metadata so
FK sorting works outside the web process. Production was untouched.

After correction, read-only local checks showed 157 unpriced holdings (down from
158), three existing AAIF prices updated to the verified quote, and no newly
unavailable holdings. Settings and the TRIG holding page rendered successfully.

## London terminal ticker markers

United Utilities (`GB00B39J2M42`) exposed a provider-format mismatch: Yahoo uses
`UU.L`, while the exact OpenFIGI London listing uses `UU/`. The previous fallback
converted that terminal slash to a hyphen and rejected the otherwise correct
identity. London-only normalisation now removes the terminal marker before matching;
internal share-class slashes remain significant (e.g. `BT/A` → `BT-A.L`). Venue
and exact-ISIN checks still apply.

Five listing-verification tests passed, including this regression and negative
venue/share-class cases. Live provider validation then succeeded. A fresh backup
was verified by restoration; the automatic correction succeeded on the restored
copy and on all four local UU account holdings. Original-data fingerprints remained
unchanged (35,631 rows across 31 non-derived tables). No schema migration or
production change was made.

## Same-ISIN currency lines

`IE00B6YX5D40` has verified London quotes `USDV.L` (GBP) and `UDVD.L` (USD).
The legacy `USDV` holding has no recorded currency, so an ISIN-only selection
previously left both candidates and required manual review. After hard source
constraints and venue preference, matching now prefers an exact broker trading-line
symbol, then an explicitly recorded provider symbol, before applying a catalogue
currency preference. Only independently verified candidates participate. Explicit
source currency and exchange cannot be overridden by these preferences, and other
share-class/trading-line symbols are not collapsed.

The full Docker suite passed (288 tests, 31 subtests), including USDV/UDVD selection
and source-currency precedence. A fresh backup was verified by restore. All four
local USDV account holdings matched and priced successfully, first on the restored
copy and then locally, with original-record fingerprints unchanged. The apparent
trailing non-breaking space was formatting, not part of the stored ticker. No
migration or production change was made.

## Conflicting secondary Yahoo ISIN lookup

For `AVGO`, direct Yahoo quote metadata identifies Nasdaq/USD but supplies no ISIN.
The secondary `get_isin()` lookup returned `CA11134P1009` (Broadcom Canadian DR),
while the held US common stock is `US11135F1012`. The adapter previously trusted
that secondary result and never tried the independent expected-ISIN fallback.

When direct instrument metadata has no valid ISIN, an exact OpenFIGI match for the
expected ISIN, quote exchange MIC and ticker can now resolve a conflicting secondary
lookup. The conflicting ISIN and secondary source are retained in evidence. A valid
conflicting ISIN from direct instrument metadata is never overwritten. Missing,
wrong-venue or wrong-symbol OpenFIGI evidence still fails verification.

All nine provider-verification tests passed, including these positive/negative
cases. Live provider verification succeeded for AVGO/US11135F1012/XNAS/USD. A fresh
backup was restored and all three AVGO account holdings matched and priced on the
copy, then locally; original-data fingerprints remained unchanged (35,631 rows
across 31 non-derived tables). No migration or production change was made.

## Madrid exchange recognition and BBVA identities

The held BBVA catalogue entries carry Spanish ordinary-share ISIN `ES0113211835`
but legacy NYSE metadata and a `BBVAe` symbol hint. The NYSE ADR is a distinct
security (`US05946K1016`); Yahoo's secondary ISIN lookup also returned the unrelated
Colombian `COB13PA00019`. The expected-ISIN fallback could not verify the correct
Madrid quote because Yahoo exchange `MCE` was not recognised.

Yahoo `MCE`/Madrid now maps to MIC `XMAD`, provider suffix `.MC` and OpenFIGI venue
`SM`. The ticker-enrichment exchange alias also recognises `MCE`. Exact ISIN and
venue checks keep `BBVA.MC` Spanish ordinary shares separate from NYSE `BBVA` ADRs.
Prices can be converted explicitly into the pre-existing display denomination;
shared metadata, quantities, costs and history are not rewritten.

All 11 provider-verification tests passed, including Madrid-vs-ADR negative cases
and the conflicting Colombian secondary lookup. Live verification succeeded for
`BBVA.MC`/`ES0113211835`/`XMAD`/EUR. A fresh backup was verified by restoration;
both local BBVA account holdings matched and priced on the copy, then locally,
with all original-data fingerprints unchanged. No migration or production change
was made.

## ASX, Milan and Stockholm coverage

BHP (`AU000000BHP4`), Enel (`IT0003128367`, legacy `ENL`) and H&M B shares
(`SE0000106270`, legacy `HMSB`) exposed missing venue coverage. Yahoo's ASX, MIL
and STO codes now map to XASX/AU/`.AX`, XMIL/IM/`.MI` and XSTO/SS/`.ST`,
respectively (MIC/OpenFIGI venue/provider suffix). MIL and STO are also recognised
by the enrichment exchange alias map.

Stockholm OpenFIGI tickers concatenate A/B share-class letters (`HMB`), while Yahoo
uses a hyphen (`HM-B.ST`). The venue-scoped conversion preserves the class letter;
H&M A shares cannot match H&M B shares. BHP's Australian ordinary ISIN remains
distinct from the US ADR ISIN. Enel's conflicting secondary Chilean ISIN can be
resolved only by exact expected Italian ISIN, Milan venue and ticker evidence.

Live validation succeeded for `BHP.AX`/AUD, `ENEL.MI`/EUR and `HM-B.ST`/SEK.
The full Docker suite passed (296 tests, 31 subtests), including wrong-venue and
wrong-share-class regressions. A fresh backup was restored and all 12 related
account holdings (including legacy BHP/BLT aliases) matched and priced on the copy,
then locally, with original-record fingerprints unchanged. BHP, ENL and HMSB holding
pages each returned HTTP 200 after restart. Currency conversion affects only the
derived current market quote; recorded costs, quantities, shared metadata and
history remain unchanged. No migration is required.
