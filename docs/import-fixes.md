# Initial import and snapshot fixes

## Verified cross-provider holding views

Holding detail now groups verified security aliases in the read layer while
preserving their original account/ticker records. Quotes convert per account,
filters apply consistently, and trades, dividends and recorded history include
linked aliases. Either alias URL opens the shared investment view. See
[verified-holding-view.md](verified-holding-view.md) for identity guards, limits and
preservation checks. No migration or financial-row rewrite is required.

## Legacy London Yahoo symbols

Bare pence-denominated symbols with missing exchange metadata now prefer `.L`
for enrichment and price downloads. GBP account/settlement currency alone does
not trigger this new rule, and explicit US exchange hints remain authoritative.
Revalidation and enrichment preserve an existing explicit suffix when incomplete
venue metadata would otherwise reduce it to a bare symbol. Verified symbol-only
metadata correction is now supported through the existing audited review service.

Local audit found eight bare pence symbols. Seven were independently verified
against their expected ISIN and London/pence quote: CUKX, EMIM, FWRG, PCT, SMT,
TRIG and UKW. Existing broker aliases supplied lookup hints only. After verified
backup restoration and rehearsal, the same seven audited corrections were applied
locally. Original financial/source fields across 38,793 rows in 34 tables remained
unchanged; only catalogue ISIN/symbol/exchange metadata and its update timestamp,
personal derived mappings and proposal/audit receipts changed. CUKS lacked a known
identity hint and was left without a catalogue correction. Shared/unassigned
foreign references remain protected by the existing exclusivity guard.

335 Python tests and 43 subtests passed in local WSL PostgreSQL, including pence
candidate selection, US/GBP behaviour, suffix preservation, price download symbols
and audited symbol-only apply/undo. No production changes or new migration.

## Analysis holding account badges

The ACC. column uses contributing platform IDs from each holdings query and
shows a separate provider/account badge per account (T212, II, CS, BI plus wrapper).
API and CSV Trading 212 delivery use the same provider code. Combined, multi-account,
wrapper, selected-pie and outside-pie queries retain their actual account IDs;
merging rows unions IDs instead of reducing them to the legacy "Both" label.
Badge metadata is owner-scoped and account names appear as tooltips. Unknown
ownership is displayed as Unassigned, without inferring a provider from a ticker.
The holding page's dividend payments and Transactions tables use the same badge
helper, keyed by each event's platform ID rather than the current holding or its
deprecated account string. Seven account-display/query integration tests and six
subtests passed in local WSL PostgreSQL, including both event tables, mixed
providers, unassigned legacy records and selected-account filters.
No schema or financial values change. WSL PostgreSQL validation passed 327 tests
and 43 subtests, including account/pie filter and ownership checks; a read-only
local Analysis render returned 200 with badges for all 429 priced holdings.

## Analysis history totals

The history chart carries each account's last recorded value across missing
snapshot dates, leaves unknown opening coverage blank, and adds a labelled Current
point from the market value card. Display currency and outside-pie aggregation
are corrected. No history is rewritten. See [analysis-history.md](analysis-history.md).

## Snapshot currency and valuation evidence

Additive migration 047 freezes price currency, unit scale, applied GBP FX and
valuation evidence for new API/CSV holding snapshots. Historical reads no longer
interpret prices using current catalogue currency. Unknown legacy units remain
unassigned, with display derived from preserved GBP totals. See
[snapshot-valuation-evidence.md](snapshot-valuation-evidence.md) for preservation,
validation and deployment details.

## Legacy cash identity blocking Data Sync

Trading 212 cash sync now reconciles an unassigned legacy transaction only from exact owned-account API evidence, preserves its PK/financial fields, and retains grouped before/after receipts. Genuine ambiguities remain untouched without aborting unrelated cash sync. A narrowly recognised legacy deposit-to-interest category difference is recorded without changing the stored type. All 150 local unassigned cash records were verified and scoped safely; CSV and non-cash guards remain strict. See `legacy-cash-sync.md` for policy and preservation checks.

## Analysis performance

Current portfolio queries calculate guarded prices once per owned position in a materialized CTE, avoid expensive PostgreSQL JIT compilation for these small projections, and restore the previous transaction setting afterward. Whole-portfolio Analysis reuses its initial native rows rather than running the same valuation query twice. The measured local request fell from 44.6 to 0.8 seconds without changing values or missing-data rules. See `analysis-performance.md` for profiling, equivalence checks and validation.

## Automatic verification of existing ISINs

Legacy catalogue ISINs now initiate independent provider verification instead of requiring statement re-entry first. Same-ISIN catalogue aliases provide discovery hints; only an exact provider identity match and an unambiguous compatible listing can create the derived account resolution. Imports, scheduled refreshes and customer price/attention controls use the common policy. No migration or original-data rewrite is introduced. See `automatic-isin-matching.md` for constraints and the AAIF regression.

## Customer holdings needing attention

Ordinary users can now review their own unpriced holdings in Settings, confirm statement identity, verify/select a listing, and apply the match with a separate price refresh. Catalogue hints require confirmation; retained contradictions and stale reviews cannot apply. Legacy missing-currency cases can display newly verified market quotes in GBP without changing historical amounts or shared metadata. No new migration/backfill is introduced. See `holding-attention.md` for the local audit, safeguards and validation.

## Verified account prices

Migration 046 adds a derived quote cache for verified holdings scopes. Account-aware reads now use explicit provider identity, quote units, observation/fetch times, FX conversion and source/revision guards; combined prices are quantity-weighted. Settings exposes owner-scoped quote refresh, and matched imports refresh separately from the committed upload. Purchase costs, legacy prices and historical snapshots stay unchanged. See `account-listing-prices.md` for expiry rules, migration preservation evidence and local comparison results.

## Account/source identity resolution

Migration 045 adds account/source-scoped resolutions, source-claim conflict auditing, actor/owner-separated audit history, signed verification reviews and bounded restartable admin batches. Shared catalogue links and financial rows remain unchanged. See `account-source-identity.md` for evidence rules, preservation checks, lifecycle behaviour and remaining financial-read cutover work.

## Stable listing identity preparation

Additive migration 044 introduces security/listing IDs, namespaced provider mappings, audited legacy alias links and an administrator verification/audit page. Existing financial rows and ticker keys are untouched. The Yahoo provider now returns prices for every local alias sharing a symbol instead of retaining only the last alias. Financial reads have not switched to the new identity layer. See `stable-listing-identity.md` for rollout scope, preservation proof and remaining cutover work.

## Import provenance and explicit source authority

Migration 043 and the staged import service add retained files/rows, before/proposed/applied observations, explicit effective dates, account/pie source authority, account locking shared with API sync, and Settings preview/apply/history controls. Transaction history defaults to event-only import; older or conflicting snapshots and stale previews cannot replace current state. The obsolete standalone `SyncRunner(account=...)` call is fixed. See `ingestion-provenance.md` for policies, limitations, preservation evidence and rollout requirements.

## Administrator metadata reconciliation

Shared ISIN and missing-price-currency corrections are exposed through **Admin → Instruments → Review statement metadata corrections**, separately from personal price mappings in Settings. The administrator supplies a statement ISIN, recorded price units, provider symbol and dated source reference. A provider identity/currency/venue check produces a before/after preview; apply changes the catalogue metadata and creates a verified personal mapping atomically. Existing quantities, transactions, original costs and source files are not rewritten. Before/after metadata and statement evidence are retained in the existing proposal/audit JSON records, with guarded undo. No schema migration or automatic data repair is introduced.

The initial workflow is limited to instruments in the administrator's own holdings. It refuses shared-user or unassigned references, even historical ones, and refuses conversion of an existing nonempty recorded currency. Those cases require wider account/history reconciliation. Administrator authorization is enforced for proposal creation, application and undo, including requests through personal mapping endpoints. Undo refuses to overwrite intervening catalogue changes. Quotes predating a metadata correction or its undo, and unverified historical broker prices, cannot be silently reinterpreted under corrected units or identity. A current verified mapping or subsequently refreshed Yahoo quote is required.

Verification: **196 Python tests and 21 subtests passed** against disposable PostgreSQL in WSL Docker; **four Node UI flow tests passed**. Tests cover missing currency, corrected identity, unchanged quantities/costs, failed verification, admin enforcement, shared-user rejection, apply/undo, stale-price rejection and newer catalogue edits. Existing local data has not been corrected automatically. Production is untouched.

This change includes **additive index migration 039**, with no historical row updates or backfill. Existing production records, IDs and relationships remain in place. Nothing has been deployed or run against production.

Changes:

* ii transaction imports now commit successful files instead of returning after a flush.
* T212 and ii transaction files roll back in full on processing, database, projection or commit failures. Processing stops and returned saved counts are zero. T212's in-memory instrument cache is cleared after rollback.
* ii dividend validation errors also reject the file instead of silently committing other rows. Existing skip behaviour for unrecognised/empty rows is unchanged; this is not yet full file validation.
* CSV snapshot upserts infer platform-based unique index columns rather than referencing an index as a named constraint. Local PostgreSQL testing revealed that migration 033 left the original account-based indexes in place: migration 039 corrects this by creating platform-based indexes first, preserving legacy uniqueness for unresolved rows, then dropping the obsolete indexes. Duplicate platform keys abort the transactional migration without deleting or changing records. Snapshot logic was moved to `app/snapshots.py` so it can be tested without starting or configuring the web application. Valuation rules are unchanged in this patch.

These fixes do not recover records lost or overwritten by earlier imports. Re-import or repair requires a separate reconciliation against original source files. Account-scoped identity, currency semantics, safe holdings snapshot replacement, and the broader model refactor remain outstanding.

## Verification

Run `pytest tests/ -v` for unit regressions. PostgreSQL checks are opt-in and skipped unless `TEST_DATABASE_URL` is set. They require a disposable local PostgreSQL database whose name ends in `_test`; the test code rejects other URLs. Do not use production credentials.

CI now creates a temporary PostgreSQL 16 service, runs `alembic upgrade head`, then exercises real commit visibility, repeat import, rollback preserving existing data, and daily snapshot upserts against the migrated index. Test cleanup deletes only rows owned by each generated test user and the test instrument.

Local verification on 1 November 2026: **109 tests passed, none skipped**, using Python 3.12 and PostgreSQL 16 in WSL Docker. A separate temporary PostgreSQL container was used with no existing data volumes; the repository was mounted read-only into the test runner. The run exercised migrations 001–038 followed by corrective migration 039. Additional migration tests verified unchanged pre-existing snapshot values (including unresolved legacy rows) and transactional rejection of duplicate platform keys. Existing application/database containers and production were not modified. This does not replace a rehearsal against a restored production backup.

Before production deployment, require CI success, a verified restorable database backup and a staging smoke test of an ii transaction upload, a rejected transaction file, and a daily CSV snapshot. The existing deployment workflow deploys pushes to `main`; keep this patch off `main` until those deployment requirements are met.

Rollback is an application rollback: retain the corrected indexes. Migration 039 explicitly refuses schema downgrade because restoring account-based uniqueness could conflict with subsequently recorded platform data. Successfully committed imports after release remain in the database. Rolling back the application does not reverse those imports. Index creation takes database locks; rehearse with production-sized data and schedule an appropriate write pause before production migration.

## Second batch: CSV parsing and snapshot safety

No additional schema migration or historical data rewrite is required for this batch.

* T212 headers retain distinct price, total and withholding-tax currencies. Semantic parenthetical fields and percentage columns remain distinct; duplicate normalised column names reject the import. Numeric currency suffixes and the legacy UTC time alias remain supported.
* ii holdings and T212 pie snapshots validate every holding before replacing data. Missing required headers, malformed or non-finite quantities, duplicate holdings and invalid supplied numeric values reject the file. Recognised blank/total rows are skipped; empty or summary-only snapshots cannot clear existing holdings. ii price/average-price unit mismatches are rejected instead of storing incomparable values.
* Pie percentage fields respect explicit percent units, including negative returns and allocations at or below one percent.
* Pie parsing performs no database writes. Instruments and the pie are created only after all rows validate. Database, reconciliation and commit failures roll back replacements and return zero saved counts. ii holdings failures follow the same rollback rule.
* T212 transaction and pie uploads retain an existing ISIN when the incoming value is missing or different. This protects existing identifiers; resolving conflicting instrument identity remains part of the planned mapping refactor.
* ii Yahoo ticker fallback values are now bound SQL parameters rather than interpolated SQL fragments.

Verification on 1 November 2026: **123 tests passed, none skipped**, plus 10 parameterised subtests, in the isolated WSL Docker PostgreSQL 16 environment. Tests cover real snapshot replacement, preservation after invalid input or failed reconciliation, separate-account isolation, identifier preservation, and full multi-currency headers. Migrations 001–039 were also exercised on an empty test database. The temporary database was removed afterward; existing local application/database containers and production data were untouched.

Remaining limitations: a syntactically valid but incomplete export cannot yet be distinguished from a complete snapshot, and files do not yet carry an authoritative effective date. Import only complete current snapshots until provenance/coverage tracking is implemented. The broader valuation-currency and account-scoped external-ID changes remain outstanding; this patch does not recalculate historical costs or prices.

## Third batch: current market valuation

This batch adds no schema migration or historical backfill. Saved past snapshots and raw imported costs/trades are retained unchanged.

`app/valuation.py` now supplies a shared contract for native listing prices, quote selection, FX conversion and holding valuation. Dashboard totals, analysis queries, pie/outside-pie values, holding detail, API snapshots and CSV snapshots use the same native-price selection. Stored `price_gbp` is never substituted for a native price. GBP/GBX quote-unit differences are converted explicitly before downstream conversion to display currency. Quote rows in incompatible currencies are excluded.

Quotes received within 24 hours take priority over older rows; within that window provider priority is retained. When only older quotes exist, the most recently updated compatible quote is used as the last-known price. Existing timestamps record ingestion/update time rather than a full exchange observation history; this remains a limitation. Broker position current prices remain a fallback; average purchase cost is no longer a market-price fallback.

FX uses one shared one-hour cache. Missing, expired, non-finite or non-positive rates never become 1:1 conversion rates. Missing market prices or FX produce an explicit valuation-unavailable HTTP 503 response on affected web views, rather than misleading zero/partial totals. Snapshot failures roll back and retain previous saved observations. Aggregated market values propagate incomplete price coverage instead of summing only priced holdings.

Yahoo prices are tagged using Yahoo's reported quote currency, including GBp/GBX. If the provider cannot identify the quote currency, it skips that price. Missing FX leaves the optional GBP equivalent unset while retaining the correctly denominated native quote. Actual dividend payments, which are stored in GBP under the existing model, are no longer converted a second time when computing aggregate dividend totals.

CSV daily snapshots now replace today's holding rows atomically, removing holdings sold since an earlier capture. Previously observed accounts that become empty receive a zero account total. Only the current day's observations are replaced; prior history is untouched.

Verification on 1 November 2026: **140 tests passed, none skipped**, plus 18 subtests, using isolated WSL Docker and PostgreSQL 16. Checks include GBP/GBX/USD values across dashboard, analysis, pies and API/CSV snapshots; fresh versus stale quote priority; Yahoo-reported units; missing/expired FX; missing and partially covered market prices; rollback preserving previous snapshots; and same-day fully sold accounts. Original migrations 001–039 also succeeded against an empty disposable database.

Separate work still required: CSV execution prices and average costs have legacy currency ambiguity, and cost rebuilding still averages historical buys across disposals. This batch fixes current **market values** and common FX handling; it does not repair historical execution/cost-basis currencies or certify existing profit calculations. Those changes need explicit source/currency metadata and a reconciliation plan, preserving original values. Account-scoped external identity and production-backup rehearsal also remain outstanding.

## Fourth batch: execution provenance and chronological cost

Migration **040** adds nullable execution price/currency, settlement amount/currency/date, source FX rate and charge fields to orders; separate cost amount/currency/status/source/reason fields to positions; and a table retaining versioned raw CSV trade observations. It performs no UPDATE or DELETE of existing records and makes no guesses about their currencies. Existing `fill_price`, `average_price`, IDs, quantities and relationships survive unchanged. Re-imported T212 trades populate explicit fields while retaining the old `fill_price`; cost projections never replace legacy `average_price`.

New T212 CSV trades use their stated execution price and currency instead of storing settlement total divided by quantity in a native-price field. Settlement currency must be supplied explicitly in a currency column or unit-bearing header; missing metadata results in unknown cost. Charges retain their amount and currency individually, and original rows are saved with a payload hash so repeated uploads do not duplicate observations. Changed versions of a row remain retained. Reported settlement cash totals are the acquisition amount used by the cost policy; separately listed charges are not added to that total a second time. Source FX rates are retained without assuming their direction or using current rates to reconstruct historical amounts.

The calculator processes dated executions chronologically using decimal moving-average costs. Partial disposals remove cost proportionally; a full disposal empties the pool so a repurchase starts at its new cost. Returns of capital, missing or ambiguous times, mixed acquisition currencies, missing settlement information and missing opening purchases prevent a fabricated cost estimate. Unsupported security movements such as stock splits/transfers reject the T212 transaction file atomically rather than being silently treated as cash. This is a portfolio cost policy, not a tax-lot calculation or realised-gain engine.

Transaction-derived costs are labelled **estimated**, because a CSV does not prove opening history is complete. A new transaction-derived position can follow its trade history; existing broker/pie/legacy quantities remain authoritative. Matching legacy quantities can acquire a separate estimated cost projection without changing the saved average price or quantity. Mismatched or incomplete histories retain the existing holding and return a reconciliation warning. Transaction-derived positions that become fully sold can be removed from the current projection; their orders and raw observations remain retained. Holdings-only positions absent from the order history are no longer deleted by a rebuild.

New API and ii holdings refreshes populate **reported** broker costs separately. ii transaction imports now also retain structured executions, settlement dates and raw rows (including SEDOL), while continuing to store cash transactions and leaving holdings snapshots authoritative. ii cash amounts follow the existing GBP export contract; price currency comes from the actual price cell. No ii position rebuild is triggered by transaction uploads.

Dashboard, analysis, holding details, trade history and AI payloads use explicit cost/execution data. Unknown costs and related profit totals are unavailable rather than zero; estimated costs carry a visible explanation. Pie costs are allocated using each account's selected quantities before conversion, so account currencies are not mixed. Trade cash values use reported settlement totals rather than ignoring fees. Non-GBP settlements and costs use current display FX when shown in another currency; historical FX attribution remains separate work. API order updates and T212 CSV trade updates reject cross-account ID collisions; the broader external-ID schema migration is still needed to support legitimate same-ID records in multiple accounts.

Verification on 1 November 2026: **160 tests passed, none skipped**, plus 18 subtests, in isolated WSL Docker with PostgreSQL 16. Migrations 001–040 succeeded on an empty database. Additional migration testing verified that pre-existing order prices, position averages and quantities stay unchanged and new provenance remains unknown. Regressions cover full sale/repurchase, partial sales, fee-inclusive settlement cost, ambiguous history, raw-row retention and repeat uploads, ii settlement/SEDOL retention, legacy preservation, failed projection rollback, cross-account guards, reported API costs, and rendering unknown/estimated costs in the actual Jinja views.

Deployment must still be rehearsed against a restored production backup. After migration, legacy cost/profit fields intentionally display unavailable until an API/holdings refresh or a reconciled CSV import supplies explicit provenance; the original values remain stored. Do not run a bulk guessed-currency backfill. Review costs and quantities per account before enabling regular imports/sync on the migrated production system.

For rollback, retain migration 040 and its provenance table. Its downgrade deliberately refuses to discard audit data. An older application that ignores the new fields can misinterpret newly imported data, so rollback requires a compatible release that retains the explicit cost reads, or pausing imports/sync while a compatibility fix is applied. A database restore must reconcile writes since the backup; it is not a lossless substitute for that application rollback plan. Nothing in this batch has been deployed or run against production.

## Fifth batch: account-scoped provider identity

Migration **041** adds `source_namespace` to orders, transactions, dividend payments and pies. It replaces per-user external-ID uniqueness with `(user_id, platform_id, source_namespace, external_id/reference)` uniqueness. Existing external strings/numbers, surrogate primary keys, amounts, raw observations and every child relationship are retained. No rows are deleted, merged, renumbered or reassigned to another account.

Existing account records receive namespaces from their recorded platform type: T212 API and CSV share `t212`; Interactive Investor uses `ii`; recognised other broker types retain their provider namespace. CSV-created pies on non-API platforms use `t212_csv_pie`. API-platform pies remain in `t212`. This is a metadata classification from existing platform information, not fuzzy matching or recovery of previously overwritten records. Unknown providers and unassigned accounts retain `legacy` provenance.

Known-source records require a user and account. A composite account/user foreign key prevents recording data against another user's account. Before changing schema, the migration checks existing ownership conflicts and stops transactionally if it finds any; it does not choose an owner. Unassigned legacy rows retain a partial uniqueness boundary and remain unassigned. A matching unresolved legacy identifier blocks a new import or sync, including otherwise nonfatal order-history steps, so the application cannot silently create a second copy or claim that reconciliation succeeded.

All import and API upserts now target the scoped identity. Legitimate identical IDs in different accounts coexist instead of being rejected or moving an existing row. T212 API and CSV deliveries with the same explicit ID in the same account update the same internal record and preserve its raw CSV observations. Broker namespaces distinguish unrelated same-ID events within an account. Existing synthetic ID/reference formats remain unchanged for repeat-import compatibility; this patch does not solve indistinguishable equal-value transactions with no provider ID, or heuristically match API references to independently synthesised CSV dividend references.

API cursors for orders, cash transactions, dividends and pie freshness are account/source scoped. New API cash transactions now receive their owning account. Dividend allocation rebuilds select only the current account/source. CSV pie IDs have a separate namespace from API pie IDs, and acceptance of a CSV pie targets that namespace. An existing same-named pie whose source cannot safely be treated as a CSV snapshot is retained and produces a reconciliation error instead of being overwritten or duplicated automatically.

Use the read-only audit before and after migration on a restored production copy:

```sh
python scripts/audit_identity.py
```

The script supports the pre-041 platform schema and the migrated schema. It reports row counts, unassigned accounts, missing users, ownership conflicts and legacy-source counts, without printing raw identifiers or credentials. It performs no writes. Ownership conflicts return exit code 1. Unassigned counts require review before enabling imports/sync: historical API cash transactions may have no account because earlier writers omitted it, and those records cannot reliably be attributed from the ID alone.

Verification on 1 November 2026: **171 tests passed, none skipped**, plus 21 subtests, in isolated WSL Docker with PostgreSQL 16. Migrations 001–041 succeeded on an empty test database. Separate populated-schema tests verified preservation of primary keys, values and child links; rejection of invalid ownership; and uniqueness across both accounts and namespaces. Application checks cover independent same-ID cash/dividend/order/pie records, repeated imports, overlapping API/CSV order deliveries, account/source cursor isolation, synthetic CSV/API pie collisions, and transactional rejection of unresolved legacy identities. The audit was also exercised against the disposable database. The temporary database was removed; production and existing local databases were not modified.

Deploy this application and migration together during a bounded write pause, including background jobs. The old per-user conflict targets are removed, so older application versions cannot continue writing against the new schema. Do not use a schema downgrade to restore those constraints: legitimate records can now share external IDs across accounts. Rollback requires a compatible application release using the new scoped targets, with imports/sync paused during cutover. A verified backup restore and account-level reconciliation rehearsal remain mandatory before production deployment; this patch has not been deployed.

## Missing-price page regression

### Settings verification feedback

Failed symbol verification previously rendered the same selection button used for ambiguous verified listings, and the completion message implied an Apply action existed. Settings now offers selection only for ambiguous verified proposals, explicitly explains why a failed proposal cannot be applied, and reports success only when an applicable proposal exists. Expired and superseded reviews request fresh verification. Three Node DOM regression tests exercise selection followed by failed, successful and ambiguous verification (`node --test tests/reconciliation_ui.cjs`); all pass in local WSL Docker. This presentation fix makes no database changes and does not bypass identity or currency validation.

The strict valuation checks originally returned HTTP 503 for an entire analysis page when any holding lacked a usable quote. This was a presentation regression: incomplete valuation must not make the holdings inaccessible.

Analysis now separates priced and unavailable holdings after applying filters. It lists every unavailable holding, its quantity and the missing price/FX reason. Complete market value, profit totals and portfolio percentages remain unavailable when coverage is incomplete; available chart data and subtotals are explicitly labelled as priced holdings only. An unpriced asset outside the selected account cannot block that account's analysis. Pie/outside-pie row merging propagates missing values instead of throwing or substituting zero. The portfolio overview likewise renders unavailable account values and a labelled priced subtotal rather than failing the entire page. Snapshot writes and financial calculations retain their strict validation; this change does not substitute cost for price or save incomplete history.

Read-only inspection of the existing local database found SHEL with ISIN `GB00BP6MXD84`, recorded GBP currency, a bare Yahoo `SHEL`/NYSE mapping, no instrument-price row, and no current position price. The stored fallback/enrichment data is not sufficient to safely assign a listing currency or reuse that fallback as a verified quote. No mapping or pricing data was changed. Other CSV holdings also lack usable quotes and remain explicitly listed for reconciliation.

Verification: **176 tests passed, none skipped**, plus 21 subtests, in WSL Docker/PostgreSQL 16. New regressions cover mixed priced/unpriced holdings, all-unpriced selections, unavailable FX, labelled subtotals, unchanged holding values, and complete selected-account views when another account is unpriced. Both corrected page renderers were also exercised against the user's existing local data inside PostgreSQL READ ONLY transactions, returning HTTP 200 without persistent writes. The local web container was restarted after confirming no sync was active; the database was already at revision 041 and no new migration was introduced. Production remains untouched.
