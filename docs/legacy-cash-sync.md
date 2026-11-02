# Legacy cash transactions and Data Sync

The reported `UnresolvedIdentityError` came from 150 existing cash transactions
whose account was never assigned by the older data model. The generic guard
prevented duplicate events, but aborted the whole API sync at the first matching
legacy reference. Pies, orders and dividends had no corresponding unresolved rows
in the local audit.

## Resolution policy

For cash transactions only, a known Trading 212 API account can now claim an
existing owned legacy row when the provider identifier, amount and exact UTC
timestamp match, and the transaction type is compatible. No account is guessed
from a display label, default account or currency. Other users' rows and rows
assigned to another account cannot be moved.

Types normally match exactly. The sole supported legacy category exception is
non-negative `DEPOSIT` versus broker `INTEREST_ON_FREE_CASH`: the legacy app stored
some interest as generic cash inflow. The stored type is preserved, and the
provider's precise category is retained in evidence. Other type differences,
negative deposit/interest pairs, missing details, multiple matching legacy rows
or a pre-existing scoped copy are deferred without inserting another copy.

Assignment preserves transaction PK, external identifier, user, amount, timestamp,
reference and notes. Only `platform_id` and `source_namespace` change. The ordinary
sync can subsequently refresh `synced_at`. Existing CSV imports and the guards for
orders, dividends and pies remain strict; this exception requires authenticated
API account evidence.

One `t212_identity_reconciliation` ingestion receipt per account/run retains the
original row, proposed assignment, API event and match basis for each adoption.
It is committed with the scope updates and normal upserts; rollback removes both.
Receipts appear in Import history with collapsed technical evidence and no CSV
matching/apply actions. No schema migration is required.

Deferred cash events no longer abort unrelated sync work. The final settings sync
message reports remaining legacy records rather than implying that they were
resolved. While legacy cash records remain, the transaction stage replays available
API history instead of advancing a cursor past unresolved older events. Normal
account/source-scoped cursors resume once legacy rows are resolved.

## Validation and local correction

- 313 Python tests and 43 subtests passed in WSL Docker; 24 JavaScript tests passed.
- Tests cover PK/financial preservation, UTC equivalence, missing/conflicting
  evidence, duplicates, provider/account/user isolation, rollback and retry.
- Read-only broker history matched all 150 legacy rows to unique accounts: 121
  exact type matches and 29 deposit-to-interest category matches.
- A fresh local dump was verified by restoring it. The real captured cash
  histories replayed on that copy and then replayed again without duplicates.
  All 150 legacy rows resolved, with no original financial fields or unrelated
  tables changed.
- The local correction changed only those 150 scope assignments, preserving all
  169 original transaction rows and their financial values. Two grouped audit
  receipts were added. No new cash transactions were imported into the actual
  local system by this correction; the next Data Sync performs the normal refresh.

Production was not accessed or deployed. The full live multi-step Data Sync was
not run by the agent; the failing transaction stage was verified with actual broker
responses on a restored database. Other sync steps retain their existing behaviour.
