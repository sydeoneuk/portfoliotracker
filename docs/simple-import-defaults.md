# Simpler customer imports

The first simplification uses the existing import contracts and retained evidence,
without a schema migration or rewriting existing holdings.

- Transaction history adds activity by default; rebuilding account holdings remains
  an explicit advanced option requiring complete coverage and existing validation.
- ii statements supply dated account quantities and reported purchase costs.
  The preview explains that absent holdings will be removed from this account.
- Pie snapshots change allocations only.
- The preview shows quantity and purchase-cost changes in readable columns.
  Missing costs do not block an otherwise valid holdings import, and the preview
  explains that returns will be incomplete. Cost currency remains separate from
  quote currency; no inferred cost is introduced.
- Checksums, parser versions and original observation fields remain available in
  collapsed technical details. The reviewed-apply requirement remains enforced.

## Automatic investment matching

After applying a CSV, the browser starts a separate matching pass, one investment
per request. Applied imports also offer **Match investments** to retry a stopped
pass. Cash movements and rows without surviving investment records are excluded.

Existing, current account/source matches are reused without a provider call. New
matches independently verify an existing ISIN from retained evidence or the
catalogue. Missing legacy quote units are learned from the verified provider.
One compatible provider listing and a verified exchange are required. A supplied
source MIC must match. Symbol/name hints and shared catalogue metadata are discovery
inputs, not identity proof. See [automatic ISIN matching](automatic-isin-matching.md).
Missing identity, conflicting evidence, multiple matches and provider failures
produce a visible **Needs attention** list; the completed import stays applied.
Removed or stale matches require review rather than automatic replacement.

Matching uses existing signed reviews, source-change guards, account locks and
audit receipts. It saves account/source resolutions without changing quantities,
purchase costs, financial history or shared instrument metadata. Matching itself
requires no schema migration. The subsequent [account-pricing implementation](account-listing-prices.md)
uses migration 046 to cache verified holdings quotes and connect them to valuation.

Validation: the full Python suite passed against a disposable PostgreSQL database
in WSL Docker (257 tests, 31 subtests). Six matching integration tests were rerun
after the final source-selection/filter changes; all passed. Sixteen JavaScript
flow tests passed. Docker validation also caught and fixed cached ORM state causing
a subsequent import preview to display the preceding statement's costs.
No production deployment or data correction was performed.
