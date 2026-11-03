# Portfolio data model and ingestion review

Reviewed 1 November 2026 against the working tree, SQLAlchemy models, Alembic revisions 001–038, importers, sync runner, pricing providers, web queries, and existing tests.

## Assessment

Implementation status: the original findings below describe the pre-fix review baseline. Subsequent stabilisation and metadata work is recorded in `import-fixes.md`. Import provenance and reviewed source selection (043) are documented in `ingestion-provenance.md`; legacy historical data remains preserved rather than assigned invented source dates. The stable security/listing foundation (044) is documented in `stable-listing-identity.md`; scoped identity conflict review and bounded resolution-link backfill (045) are documented in `account-source-identity.md`. Verified holdings quote caching and account-aware valuation reads (046) are documented in `account-listing-prices.md`. Finer historical identity exceptions, general listing-level quote sharing, provider/account/connection separation and the wider canonical-event model remain outstanding.

The application has outgrown its original Trading 212 representation. The recent `platforms` work is a useful bridge, but provider identity, account identity, ingestion method, and valuation semantics are still intertwined. Several independently writable representations now describe the same holdings without recording their authority, effective date, or provenance.

The problem is broader than denormalisation. It includes lossy ingestion, identifiers with the wrong scope, implicit currencies, inconsistent transaction boundaries, and different calculation rules across screens. Normalising tables alone would preserve these defects in a more complicated schema.

Use an incremental refactor: first stop incorrect writes and valuations, then establish account and instrument identity, then introduce common ingestion and valuation services. Keep historical observations and derived views, but make their source and derivation explicit.

This is a source review, not a production data audit. No live database was queried or modified. Findings below describe executable paths and schema definitions; the number of affected production records remains unknown. Tests could not be executed: `python` resolves to an inaccessible WindowsApps alias; no usable `py`, `uv`, or Docker executable was discovered on PATH. Existing tests use helpers and mocked sessions, so they do not validate PostgreSQL constraints, persistence, or migrations.

## Findings requiring correction

Snapshot follow-up: migration 047 freezes units, applied FX and valuation evidence
for new holding snapshots; legacy unknown-unit rows remain unchanged and display
from preserved GBP totals. See [snapshot-valuation-evidence.md](snapshot-valuation-evidence.md).

Holding-view follow-up: verified aliases now resolve to an owner-scoped combined
investment view without merging financial rows. See [verified-holding-view.md](verified-holding-view.md).

### 1. P1 — ii transaction uploads report success without persisting

Evidence: `app/importers/ii_transactions_csv.py:155`, `app/web/server.py:1913`, `app/database.py:9`.

The importer ends with `flush()`. Its route returns immediately without `commit()`. The request dependency closes the session without committing. Consequently, the ordinary successful upload path rolls back its dividend and cash transaction writes when the session closes, despite returning imported counts.

Fix: put transaction ownership in an application service and commit once after a successful import. Do not patch the dependency to commit all requests indiscriminately. Add an integration test that imports through the route and reads the records from a separate session.

### 2. P1 — CSV history snapshots target a constraint removed by migration 033

Evidence: `app/web/server.py:742`, `alembic/versions/033_platform_backfill.py`, `app/models/holding_snapshot.py:8`.

Migration 033 drops `uq_holding_snapshots_daily_ticker` as a constraint and creates a unique index with the same name. `_snapshot_csv_platforms()` supplies this name as `constraint=`, generating `ON CONFLICT ON CONSTRAINT`. A standalone index is not a named constraint, so this path fails against the migrated schema. ORM metadata still declares a `UniqueConstraint`, masking the discrepancy in metadata-created databases.

Fix: use column inference for this ordinary unique index, or migrate it back to a real unique constraint. Test against a database built with Alembic, not only `Base.metadata.create_all()`. Review the portfolio snapshot expression index separately: its `COALESCE(pie_id, 0)` intentionally handles NULL scope identity and is not equivalent to the ORM's ordinary unique constraint.

### 3. P1 — prices and cost basis do not have a consistent currency contract

Evidence: `app/importers/t212_csv.py:242`, `app/web/server.py:2102`, `app/web/server.py:3145`, `app/web/server.py:3493`, `app/web/server.py:4054`, `app/sync/runner.py:654`, `app/prices/yfinance_provider.py`.

* T212 CSV stores `Total / quantity` as `Order.fill_price`, described as GBP, while API order and position prices are treated as native listing prices. The CSV does not establish the total's currency. The same field therefore has different meanings depending on the writer.
* `_PRICE_SUBQ` returns `COALESCE(price_gbp, price)`. Its output can be GBP, USD, or pence, with no output currency. Downstream callers cannot safely convert it.
* The portfolio summary divides that expression by 100 for GBX. When a yfinance row already has `price_gbp`, a holding worth £200 can appear as £2.
* The transaction page converts imported `fill_price` using the instrument currency, even when CSV import already converted it to account currency. For example, a USD listing bought for £80 can have that £80 converted as though it were $80.
* CSV history uses native `ip.price`, converts only GBX, and labels other currencies GBP. It also falls back to average cost rather than the uploaded current price.
* Pricing and display helpers sometimes default a missing FX rate to 1.0. Missing conversion becomes a plausible but incorrect value.

Fix: separate execution price/currency, settlement amount/currency, fees, taxes, and FX rate. Return typed valuation results with currency, quote timestamp, FX timestamp, and completeness. Native prices must never share a scalar fallback expression with GBP values. Missing FX or price means unavailable or explicitly estimated, not parity or cost presented as market value.

### 4. P1 — row failure can discard earlier successful T212 rows and then commit a misleading partial result

Evidence: `app/importers/t212_csv.py:152–173`.

On any row exception, `session.rollback()` discards every earlier uncommitted row. Counters and `_known_tickers` are not reset. Subsequent rows continue and the importer rebuilds positions and commits. A cached ticker may now refer to an instrument whose insertion was rolled back. A newly created platform from the route can also be rolled back. The returned counts need not describe committed data.

The ii transaction importer instead catches exceptions without rolling back a failed database transaction, so a database error can leave later operations unable to proceed.

Fix: parse and validate before applying; default to atomic file application. If partial imports are a deliberate feature, use per-row savepoints with accurate committed counts and durable rejection records. Serialize writes for the same account so overlapping imports and syncs cannot replace each other's results.

### 5. P1 — external identifiers can overwrite records from another account

Evidence: `app/models/order.py:9`, `app/models/transaction.py:8`, `app/models/dividend_payment.py:9`, `app/models/pie.py:9`; matching importer and sync conflict targets.

Orders, transactions, dividends, and pies identify external objects using `(user_id, external_id/reference)`, rather than the owning account and source namespace. Conflict updates can reassign `platform_id`, silently moving a record between accounts. Migration 036 fixes one synthetic dividend reference format but does not fix the underlying uniqueness boundary.

ii fallback transaction identity is only platform/type/date/rounded amount. Two legitimate equal deposits on the same day collide. ii dividends use platform/symbol/day, so separate distributions on the same day also collide. Synthesising a hash from those same fields would retain the collision.

Fix: distinguish internal IDs, source object IDs, and canonical economic event IDs. Scope source objects to an account and identifier namespace. Match API and CSV observations of the same event explicitly; do not let each connection generate a second economic event. Preserve repeated identical rows using source references or multiset-aware matching. Where overlapping files cannot be disambiguated, flag the ambiguity rather than silently deduplicating.

### 6. P1 — global ticker identity mixes unrelated listings and separates equivalent holdings

Evidence: `app/models/instrument.py:10`, `_ensure_instrument()` in all CSV importers, `app/sync/runner.py` catalogue writes, `app/prices/yfinance_provider.py`.

The shared instrument key is a source ticker. API identifiers such as `VWRPl_EQ` and CSV symbols such as `VWRP` can create separate records for the same listing. Conversely, a bare symbol can identify different securities on different exchanges and still hit the same global row. CSV imports can change shared names or clear an existing ISIN when their row lacks one. ii transaction import assumes GBP and `.L` even though holdings parsing supports USD and EUR.

The yfinance provider also maps each Yahoo symbol to one instrument dictionary. Multiple local aliases resolving to the same Yahoo symbol overwrite one another in that map, leaving some aliases without refreshed prices.

Fix: introduce stable security/listing IDs and provider symbol mappings. Resolve using available ISIN, exchange/MIC, quote currency, and verified mappings. ISIN alone does not identify a trading listing. Preserve unresolved rows for review; do not guess a listing from account currency. Shared catalogue changes should be validated separately from a user's holdings upload.

### 7. P1 — header normalisation loses distinct currency columns

Evidence: `app/importers/t212_csv.py:65`, `tests/test_t212_csv.py:34`.

Every trailing parenthesis is stripped. Thus `Currency (Price / share)`, `Currency (Total)`, and `Currency (Withholding tax)` all become `currency`; the last header wins. `_get()` cannot recover the original distinctions. Existing tests explicitly accept that stripping but do not test a complete multi-currency header row.

Fix: preserve semantic column names and use explicit format-specific aliases. Only strip a recognised unit suffix from columns where that suffix is a unit. Validate required headers and detect ambiguous mappings before writing.

### 8. P2 — position rebuilding gives the wrong remaining cost after sales and repurchases

Evidence: `app/importers/t212_csv.py:379`.

The query averages all historical purchases, irrespective of when disposals occurred. Buy 10 at £10, sell all 10, then buy 10 at £20: remaining cost should be £200, but the query produces £150. Missing opening history, transfers, splits, fees, and corporate actions also cannot be represented by buys minus sells alone. ii trades lose structured quantity, price, settlement date, SEDOL, and instrument linkage because they are flattened into cash `Transaction` records.

Fix: preserve typed executions and movements. Build positions with chronological processing under a documented portfolio cost policy. Treat broker-reported cost and calculated cost as separate observations. Preserve unknown opening cost as unknown. Do not present a portfolio average-cost calculation as a tax calculation.

### 9. P2 — snapshot replacement and pie acceptance can destroy valid state

Evidence: `app/importers/ii_csv.py:140`, `app/importers/t212_pie_csv.py:91`, `app/web/server.py:2004`.

ii holdings skips invalid rows then removes existing positions absent from the accepted rows. A partially malformed file can delete valid holdings. Pie import deletes previous holdings before establishing that any replacement holding is valid, so a nonempty file of invalid rows can clear a pie. Neither path preserves a source effective date, so importing an older snapshot can overwrite newer state.

Accepting pie holdings sets account quantities to the sum of pies. Any quantity held outside pies disappears for affected tickers, and a later transaction rebuild can reverse this adjustment. Neither version records why one source was chosen.

Fix: validate full snapshot coverage before replacement, retain immutable observations and effective dates, and make authority an explicit account policy. Pie allocations should reconcile against account holdings; a deliberate correction should be recorded with reason and provenance. Incomplete transaction history must not silently supersede a complete broker snapshot.

### 10. P2 — account generalisation is incomplete

Evidence: `app/models/platform.py`, `app/auth/models.py`, `app/web/server.py:538`, `app/web/server.py:4507`, `scripts/sync.py:83`.

`Platform` is actually a user's investment account. Its type combines broker and acquisition mechanism (`t212_api` versus `t212_csv`). Credentials, currency, cash, and sync status remain fixed Trading/ISA columns on `UserSettings`. Sync selects the first account by provider/type rather than a specific connection. Analysis explicitly returns zero cash for platform/platform-list/account-type filters. The legacy script still calls `SyncRunner(account=...)`, although its constructor now accepts `platform`, so that entry point is broken.

Deprecated `account` strings also remain in aggregation and display queries, with legacy `Trading`, newer `GIA`, and potentially null values. Nullable account ownership in historical tables permits records outside the new account model. Independent user/account foreign keys do not enforce that the account belongs to that user.

Fix: represent broker, investment account, and connection separately; move cash and sync state to the relevant account/connection. Replace legacy string filters after a verified backfill. If retaining `user_id` for query isolation, enforce `(account_id, user_id)` against account ownership rather than trusting duplicated values.

### 11. P2 — stale quotes and duplicated calculation paths make results depend on screen and timing

Evidence: `_PRICE_SUBQ`, `_snapshot_csv_platforms`, `SyncRunner.snapshot_portfolio_day`, `app/prices/updater.py`.

Price selection ranks sources without checking age. An old API quote can permanently outrank a fresh yfinance quote, even though the updater knows the API quote is stale. Current valuation, API history, CSV history, analysis, and transaction display each implement separate conversion rules. Daily CSV snapshots exclude accounts with no positions and upsert holdings without removing rows sold earlier that day; portfolio and holding history can therefore disagree.

Fix: one valuation service with explicit freshness and fallback policy, used by screens and snapshot generation. Replace an entire daily snapshot atomically, including an explicit zero balance for a closed account, and distinguish carried-forward quantities from newly observed holdings.

## Proposed domain model

Keep a modular monolith and PostgreSQL. Additional services or a full accounting engine are not prerequisites.

| Entity | Responsibility and important constraints |
|---|---|
| `providers` | Broker identity, independent of API/CSV delivery. |
| `investment_accounts` | User, provider, display name, account wrapper, base currency, lifecycle; preserve existing platform IDs initially. |
| `account_connections` | Account, connector type, encrypted credentials, capabilities, sync cursor/status. One account can accept API and CSV observations. |
| `securities` | Economic security/share class; validated identifiers such as ISIN. |
| `listings` | Security, exchange/MIC, symbol, quote currency, quote unit scale; stable internal ID. |
| `instrument_mappings` | Provider/namespace/external identifier to listing, with verification and provenance. Market-data mappings can use the same concept. |
| `ingestion_batches` / `ingestion_rows` | Account, source, file checksum, parser version, import time, effective/coverage dates, original row, validation status and outcome. A checksum detects exact file repeats, not overlapping history. |
| `source_records` | Account-scoped source object identity and observation versions; link API and CSV representations to canonical events. |
| `account_events` | Canonical event type, event and settlement dates, source links, correction/supersession links. |
| `executions` / `security_movements` | Listing, signed quantity, native execution price/currency, and transfers/corporate actions; optional link to an order. Orders remain instructions, not the trade ledger. |
| `cash_movements` / `income_details` | Signed money/currency, fees, tax, gross/net income, and optional security linkage. Preserve both sides of FX. |
| `holding_observations` / `cash_observations` | Broker-reported quantities, costs and balances with effective dates, completeness and batch IDs. Holdings-only accounts remain fully supported. |
| `positions_current` | Rebuildable account/listing projection recording selected source, effective date, quantity, cost currency, cost completeness and calculation version. |
| `quotes` / `fx_rates` | Listing or currency pair, provider, observed time, fetched time, unit, rate; freshness is explicit. |
| `portfolio_groups` / `group_allocations` | Generic pie/group membership and allocation observations. Provider identifiers are mappings rather than synthetic API IDs. |
| `valuation_runs` / holding valuations | Selected observation/event cut-off, quote/FX references, calculation version and missing-data status. Daily totals are derived from the same run. |

Use `Numeric`/`Decimal` for canonical money, quantity, rates and prices; determine precision from supported instruments and retain source precision. Use timezone-aware event timestamps, retaining date-only settlement/effective dates where that is all the source supplies. Separate quote units such as pence from monetary currencies. Do not manufacture precision or timezone information absent from a file.

Not every field should be normalised away. Historical snapshot quantities, prices, currencies and FX assumptions are facts about what was observed or valued then. Current instrument metadata must not rewrite those historical semantics. Materialised totals and dividend group allocations are acceptable projections if they record calculation version and can be reconciled or rebuilt. Independent writable copies without that contract are the problem.

For account authority, record separately whether holdings are broker-reported or event-derived, whether transaction history is complete, and the effective date of the latest complete observation. Reconciliation should report differences without rewriting either source. A pie-only account can be supported explicitly as incomplete account coverage.

## Application boundaries

The existing price provider interface is a useful starting point. Apply the same adapter pattern to ingestion, while keeping source-specific parsing in each adapter:

`API/CSV adapter → typed source records → validation and identity resolution → atomic ingestion service → canonical observations/events → position projection → valuation service → web views`

Extract import transaction management, account scoping, instrument resolution, reconciliation, and valuation from `server.py` and `SyncRunner`. Routes should authorise the account, invoke a service, and return results. Source adapters should not commit, overwrite shared catalogue metadata, rebuild account state, or decide which source is authoritative.

## Migration plan

### Production data preservation requirement

The production system contains live data that must be preserved. All implementation and migration work must satisfy this requirement; the plan below is not permission to delete, reset, or reconstruct production records from incomplete history.

* Use additive schema changes first: new nullable columns, tables and mappings. Retain existing records, identifiers and relationships throughout backfill and validation. Do not drop or repurpose legacy columns during the initial migration.
* Before production changes, take a consistent database backup and verify restoration into an isolated database. Rehearse the upgrade against a recent production copy, including the application's old and new read paths where compatibility is required.
* Backfill in bounded, restartable steps with explicit progress and exception reporting. Never silently merge ambiguous instruments, remove duplicates, infer missing currencies, or overwrite broker-reported holdings. Preserve original values and a mapping from each old record to its new representation.
* Reconcile row counts, ownership, relationships, quantities, cash, dividends, cost completeness and account totals before and after migration. Explain every difference; intentional calculation corrections must preserve the original source data and have a recorded derivation. Ambiguous records remain preserved and block the affected cutover until resolved.
* Coordinate application deployment and writes during backfill/cutover so imports and sync cannot race the migration. Use compatible releases, account-level locking or a bounded write pause as appropriate. Validate final deltas before enabling the new writer.
* Prefer application rollback to preserved old tables and columns. Do not depend on a destructive Alembic downgrade. If new writes cannot be represented by the previous application, provide a tested compatibility/replay path or pause those writes before rollback. A backup restore must account for writes made since the backup.
* Defer destructive cleanup to a separate, explicitly reviewed release after reconciliation and a suitable retention period. No production reset, truncation, blanket delete, or automatic deduplication is part of this refactor.

1. **Stabilise current behaviour.** Fix findings 1–7 first, including PostgreSQL integration coverage and correct currency semantics. Make snapshot replacement safe. Preserve original uploads for any repair requiring re-import. Avoid broad data rewrites based on guessed currencies.
2. **Audit existing data.** On a database copy, compare migrations with actual constraints; count null/mismatched ownership, duplicate aliases, unresolved listings, missing currencies and stale quotes. Compare uploaded source rows with persisted counts and account totals. Records overwritten by old uniqueness rules cannot be recovered from the surviving row alone.
3. **Introduce accounts and identity additively.** Treat `platforms` as the migration source for accounts, preserving IDs and links. Add provider/connection entities and stable listing IDs; backfill an explicit old-ticker mapping. Put ambiguous mappings into a review queue rather than merging automatically. Bind credentials to exact accounts.
4. **Add provenance and common ingestion.** New imports go through batch validation and atomic application, with account-level locking. Keep the existing read model behind a compatibility layer. For historical records without raw data, mark provenance as legacy/unknown instead of inventing it.
5. **Build and compare projections.** Introduce canonical events/observations and the position/valuation services. Compare quantities, costs, cash, dividends and totals per account against both the old application and trusted statements. Classify differences: the old calculation is not automatically the correct baseline. Switch one account/source path at a time.
6. **Retire duplicate authority.** Move screens and scheduled snapshots to the common service; remove old writers before removing old columns. Enforce required account IDs, scoped uniqueness, ownership constraints and scope checks after resolving exceptions. Update scripts and documentation. Version corrected historical valuations rather than silently blending them with old calculations.

Each stage should be independently deployable. Prefer one canonical writer plus a compatibility projection over indefinite bidirectional dual writes. Keep cutover reversible until account-level comparisons pass; dropping legacy columns is a later release.

## Verification required before cutover

* Build a disposable PostgreSQL database with `alembic upgrade head`; also test upgrading representative pre-platform data. Verify the actual constraints and conflict paths.
* Import via HTTP and read through another session, including ii dividends and transactions. Verify persistence and truthful counts.
* Re-import the same file, overlapping exports, repeated identical payments, and the same external ID in two accounts. Verify account isolation and economic-event deduplication across API and CSV.
* Test valid rows followed by a database failure, invalid full snapshots, older snapshots, and concurrent imports. Preserve valid committed state under the chosen atomicity policy.
* Test GBP, GBX, USD, multiple currency headers, unavailable FX, stale API versus fresh market data, and GBP converted prices. Every screen and daily snapshot should agree for the same valuation run.
* Test a full sale followed by repurchase, partial disposals, opening balances, transfers and split adjustments. Distinguish unknown from zero cost.
* Reconcile account holdings against multiple pies plus outside-pie quantities. Reject account ownership mismatches at the database boundary.
* Replace same-day snapshots after a sale, and capture a zero-holdings account. Holding-level totals must reconcile with account totals and the stated cash inclusion policy.

The first implementation should be the stabilisation work and real database tests. The architectural destination is a provider-independent account model with explicit observations, events and valuation provenance; it can be reached without rewriting the entire application at once.
