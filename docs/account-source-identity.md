# Account/source identity resolution and reviewed backfill

Additive migration **045** adds `account_listing_resolutions` and `account_listing_audits`. A resolution binds **account + source namespace + legacy ticker** to a stable listing. It does not replace the global catalogue alias. The same bare ticker can therefore identify different securities or venues in different accounts or sources without rewriting their existing records.

## Admin workflow

Open **Admin → Instruments → Review account/source identity conflicts**.

1. Search by account name, ticker or catalogue ISIN. The default view prioritises aliases currently held; uncheck the filter for historical-only references.
2. Review retained identity claims, their evidence references and shared catalogue hints. Conflicting claims within one scope block resolution. Different identities across accounts are flagged but can be resolved independently.
3. Enter the statement ISIN, listing quote units, optional statement MIC, provider symbol and dated statement reference covering the scope. Missing identity is not inferred from account currency, costs or a shared bare ticker.
4. Verify against the provider and review the resulting evidence. Verified scopes can be queued; verification alone writes no resolution.
5. Acknowledge the batch and apply up to **20** scopes. Each item reports `applied`, `already_processed`, `blocked` or `failed`. Each successful item commits independently; a blocked item does not roll back earlier successes.

Reviews are signed, actor-bound and valid for 15 minutes for new applications. Each review has a durable unique key. Repeating a completed request returns its recorded receipt, even after expiry or undo, without reapplying it. Pending expired or changed reviews must be verified again. A page reload loses the browser's pending queue, but completed progress remains in the audit and current scoped resolutions. Reopen the audit and continue with unresolved/stale scopes; no financial records are duplicated.

Undo is available only for the latest matching source/mapping state. It restores or removes the scoped link and records the administering actor. Stable securities, listings and provider evidence remain retained. It does not undo economic events, since applying a resolution never rewrites them.

## Source evidence and conflicts

Scopes are discovered from account-owned positions, orders, dividends and pie holdings. Holdings use the selected import/API source or `holdings:legacy`; economic records use `events:<source_namespace>`; allocations use `pie:<source_namespace>`. Namespace labels describe existing provenance and do not invent a connection or broker identity for unresolved records.

Only **applied** uploads contribute retained identity claims; pending or rejected files cannot become authoritative evidence. ISIN/MIC and explicit execution-price currencies are extracted from retained trade observations and source rows. ii holdings use the price's explicit currency/unit marker. Settlement, book-cost and dividend-receipt currencies are deliberately excluded from quote-identity inference. The retained flat API positions schema lacks reliable listing identity fields, so a dated statement assertion is required where source identity is missing.

Multiple ISINs/MICs or incompatible quote currencies within one scope require finer dated/per-record reconciliation. There is no force override of contradictory source claims. The tool also flags a ticker whose scopes contain different retained or currently verified identities, and offers other catalogue aliases sharing an ISIN as hints only. Unassigned event records are counted separately; this release never assigns an account or owner to them.

The source fingerprint uses account/owner, namespace, alias, identity claims, active state and a catalogue revision guard. Shared catalogue values are used only to detect change, not to prove a scoped claim. Ordinary quantity/price refreshes and new evidence with the same identity do not invalidate a resolution; identity, ownership, source-selection or catalogue changes do. Unknown/missing scope, stale evidence or contradictions cause the new scoped resolver to return no listing. It never silently falls back to the global alias link.

Original references and dated statements are retained in audit evidence. The admin page displays up to 30 evidence references per scope; complete source files and observations remain retained through ingestion provenance. Account ownership and provider-mapping/listing consistency are enforced by composite foreign keys. The account owner and administering actor are recorded separately, including when an admin resolves another user's account. Non-admin users cannot read or modify this audit through the new endpoints.

## Data and pricing preservation

Migration 045 creates two tables and supporting indexes only. There is no automatic backfill, legacy alias merge, instrument metadata edit, price overwrite, event deduplication, or reassignment of existing positions/events. Reviewed batches backfill **resolution links**, not financial foreign keys. Pricing and valuation still use the existing paths. Listing-level quotes and per-account shadow valuation comparisons remain the next cutover stage.

Clear Portfolio Data removes current scoped resolutions while retaining the audit, preventing old links from reviving when records are recreated. Explicit account deletion removes its scoped children in foreign-key order. Normal import/sync operations preserve identity audit history. New source mappings do not change monetary amounts or establish missing historical price-unit semantics.

For a read-only repeatable snapshot audit:

```sh
python scripts/audit_account_identity.py --search SHEL
python scripts/audit_account_identity.py --include-historical
```

The default CLI scans held scopes and prints aggregate counts only. The admin audit is paginated and makes no external provider calls; only explicit verification performs provider lookup.

## Verification and deployment

**248 Python tests and 27 subtests passed; 13 Node UI tests passed.** Coverage includes per-account/source isolation, different securities behind one alias, source contradictions, separating cost/quote currency, preservation of financial records and global catalogue, cross-user admin actions, database ownership constraints, stale claims, harmless price/quantity refreshes, token expiry, idempotent receipts, partial batches, undo, clear-data replay protection, and explicit deletion.

A verified backup of the local database was restored to an isolated test database. The restored snapshot already reported revision 045, so the two verified-empty new tables were removed only on that disposable copy and its Alembic marker set to 044 to exercise the populated upgrade explicitly. The 044→045 rehearsal preserved every original value: **35,630 rows across 33 original tables**, including retained uploads and the stable-identity foundation. No original tables were removed or altered. The first read-only audit page examined 25 of **2,265 held account/source scopes** in approximately **0.54 seconds**. The live admin page and SHEL audit, static script and TRIG holding page were checked without applying resolutions; no live scoped mappings were automatically created.

Keep the additive tables on rollback. Production still requires its own verified backup/restore rehearsal and coordinated writer pause. No production database was accessed. This is a preparation layer; do not switch financial reads until account-by-account listing/valuation comparisons and unresolved historical exceptions are reviewed.
