# Stable security and listing identity: preparation stage

Finding 6 is now being addressed through additive schema **044**, rather than replacing legacy ticker primary keys in one migration. Existing positions, events, prices, imports and history retain their original identifiers and values.

## Identity records

- `securities` assigns an internal ID to a verified ISIN/share class. ISIN is not treated as a trading-listing identifier.
- `listings` assigns an internal ID to security, MIC, monetary quote currency, unit scale and venue symbol. A GBX quote is represented as GBP with a decimal scale of 0.01. Distinct venues, symbols or quote units remain distinct listings; GBP/GBX aliases can still reference the same provider-verified listing.
- `provider_listing_mappings` records provider, identifier namespace and external identifier with verification evidence. Initial writes support Yahoo's global symbol namespace; account-specific broker namespaces are not guessed from legacy ticker spellings.
- `instrument_listing_links` is a compatibility bridge from retained instrument keys to stable listing IDs. Composite foreign keys ensure that its provider mapping belongs to the same listing. A source metadata fingerprint invalidates a link when ISIN, MIC, units or provider hints change.
- `listing_identity_audits` retains administering actor, before/after link and undo history. Removing a link does not delete securities, listings, provider mappings or verification evidence.

The bridge verifies a catalogue alias. It does not prove that every historical account record sharing a formerly global ticker belongs to that listing. Such collisions still require account/source evidence before financial cutover; no positions are consolidated using these links.

## Administrator workflow

Open **Admin → Instruments → Audit stable security and listing identities**. Search a legacy ticker, name or ISIN, enter its Yahoo symbol, verify its identity, review the evidence, then apply the alias link. Missing ISIN/currency or venue conflicts must first be resolved through the existing administrator metadata workflow.

Verification requires matching ISIN, compatible recorded units, a provider MIC and timestamped usable quote. Existing MIC conflicts are rejected. Names and bare symbols are not identity evidence. Yahoo symbols are case-normalized. The reviewed payload is signed, actor-bound, expires after 15 minutes, and contains the original source/link baseline. Apply and undo require administrator authorization independently of verification; stale or changed state is rejected. A provider symbol cannot be silently reassigned to a different listing.

Page loads and audit operations are read-only and make no provider calls. Only explicit verification requests query market-data identity sources. A new import alias remains unresolved until verified; migration does not promote stored enrichment or personal price overrides into global identity authority.

For a read-only aggregate report:

```sh
python scripts/audit_listing_identity.py
```

The report counts verified, stale and unresolved catalogue aliases and candidate duplicate groups with matching stored metadata. Candidate groups are hints, not backfill instructions. It includes unheld catalogue instruments; unresolved identity preparation does not mean that their existing market prices are unavailable.

## Pricing compatibility

Pricing, valuation and financial reads remain on the existing paths in this release. `resolve_listing()` provides a fail-closed compatibility resolver for future readers: unresolved or stale links return no listing. No historical quantities, costs, currencies or dividend histories are reinterpreted.

The standalone Yahoo provider had a separate alias-loss defect: its dictionary kept only one instrument for each Yahoo symbol. It now downloads each symbol once and emits a quote for every distinct local ticker alias, preserving the provider's actual quote currency. Duplicate copies of the same local ticker are emitted once. This does not combine holdings or create an economic-event deduplication rule.

Account/source resolution and bounded, reviewed resolution-link backfill are now implemented in migration 045; see `account-source-identity.md`. Remaining stages are finer historical identity exceptions, listing-level quotes and per-account shadow valuation comparisons. Financial reads should switch only after quantity, cost, cash and valuation differences are explained. Equivalent listings do not imply duplicate economic events.

## Preservation and validation

Migration 044 creates five tables and supporting indexes only. It performs no backfill, merge, delete, financial-row updates or automatic links. Before local enablement, a fresh backup was verified by restoration to an isolated database. Upgrade rehearsal fingerprinted all **35,630 original rows across 28 tables**, including retained upload bytes and observations, and found them unchanged. All new identity tables remained empty after migration.

**233 Python tests and 27 subtests passed**, and **ten Node UI tests passed**. Coverage includes matching aliases without position consolidation, separate venues for one security, unit scales, missing/conflicting evidence, stale and expired reviews, actor/admin authorization, provider reassignment rejection, guarded undo, fail-closed resolution, read-only audit and Yahoo quote fanout. Final targeted identity tests passed after case-normalization. The migrated live local admin page, audit, static script and existing TRIG holding page were checked successfully; no live alias links were applied. Production was not accessed.

Keep the identity tables during application rollback. Production rollout still requires a recent production-copy rehearsal, verified backup and coordinated writer pause. Removing legacy keys or merging historical aliases belongs to a later reviewed cutover.
