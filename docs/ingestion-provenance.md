# Import provenance and holdings authority

Parser `csv-2` fixes ii holdings where quote currency differs from account-currency cost. Explicit book cost takes precedence; zero book cost remains unknown, and the legacy average-price field is only populated in quote units. GBP/pence prices are normalized to the recorded listing units without converting account-currency cost twice. True quote/listing currency conflicts remain blocked. The ii `Totals` marker followed by currency summary rows is recognised; unlabelled missing-symbol rows still reject the snapshot. The retained ii upload was parsed read-only: all 13 holdings and its footer validated without listing-currency conflicts. **222 Python tests passed**, including mixed-currency and footer regressions. Existing rejected batches remain immutable; upload again to obtain a `csv-2` preview. No holdings were applied automatically.

CSV uploads now create a retained **preview**, rather than immediately changing portfolio state. In Settings, preview the file, review the source/effective date and holding differences, acknowledge coverage, then select **Apply reviewed import**. Recent previews, rejections and applied batches can be reopened through **Import history and source review**.

## Retained evidence

Additive migration **043** creates four tables:

- `ingestion_batches`: owning account/user, source, filename, exact file bytes, SHA-256 checksum, parser version, supplied dates/coverage, status, preview and application time.
- `ingestion_rows`: ordered original CSV cells, including headers. These retain source precision and duplicate header names; they are not a claim that every row became an economic event. Whole-file validation errors and adapter counters are retained on the batch.
- `import_holding_observations`: full original projection records, proposed records and actual applied records. Timestamps and legacy values remain available even when a reviewed replacement removes a position from the current projection.
- `import_authorities`: the selected observation batch/date for an account, or a particular pie allocation scope. Account ownership is enforced with composite foreign keys; source rows and observations also reference their batch owner.

Historical data is not backfilled with invented provenance. Existing holdings display `legacy/unknown` authority until a new source is deliberately selected. Nonfinite legacy values are retained as strings in evidence rather than producing invalid JSON.

## Authority rules

- ii holdings require a statement effective date. They replace the account projection only after explicit review and apply.
- T212 trade history defaults to **event-only ingestion**. It cannot silently replace broker, holdings-only, pie or legacy quantities. Complete-history projection requires an explicit user declaration and start/end dates. Source rows and existing trades must fit that range, current holdings must be explained by trades, and unsupported movements or unreconciled quantities block application. Completeness is a recorded declaration, not an automatic certification of a broker export.
- A complete-history selection may deliberately replace previous observations, but its quantity/cost differences are previewed first. Calculated cost remains estimated under the portfolio policy, not a tax calculation.
- API accounts retain broker holdings authority; CSV uploads cannot select replacement account quantities for them.
- Pie snapshots require a date and affect only that pie's allocation observation. They do not establish complete account coverage. The previous pie-accept endpoint now rejects account-quantity replacement, preserving outside-pie holdings. Pie-only accounts need a complete account observation or a future explicitly incomplete-coverage policy.
- Older observations cannot replace a newer selection. Different files for the same selected date are held for reconciliation rather than silently ordered by upload time. There is no force-overwrite button.
- Exact repeated files are identified by checksum and linked to the previous applied batch in the preview. This is not used to deduplicate overlapping economic events; existing account/source identity rules still apply.
- Application rechecks both account state and authority. A changed account or instrument identity invalidates the preview and requires a fresh upload. Application retries for an already-applied batch are idempotent.

API position refreshes retain the received JSON and before/applied observations in the same transaction as positions. Their date is explicitly the **fetch date**, because a broker statement effective timestamp is unavailable. API sync and CSV staging/application share a PostgreSQL account advisory lock that survives intermediate commits. Competing writes fail clearly and can be retried. Standalone adapter calls retain their legacy transaction behaviour for compatibility; application routes use service-managed adapters and do not let adapters commit independently.

## Data lifecycle and limits

Uploads are limited to 10 MB. Only the owning user can list, review or apply a batch. A rejected file is retained with its error and does not change current holdings; an apply failure rolls back application while retaining its preview.

Clear Portfolio Data preserves retained import evidence, resets authority and invalidates pending previews. Explicit account deletion removes the associated import children in foreign-key order. Normal ingestion never deletes source evidence. There is no automatic historical replay or migration of old imports, and no general undo of applied economic events; retain backups and review source changes carefully.

The standalone sync script now supplies an explicitly owned `Platform` to `SyncRunner`, and rejects missing/ambiguous matching API accounts instead of using the obsolete `account=` argument. It continues to use the script's legacy environment credential configuration; connection-level credentials remain a separate refactor.

## Validation and deployment

Migration 043 was tested from an empty Alembic database and on a restored local database. The populated rehearsal preserved every original value: **35,529 rows across 24 tables** were fingerprinted before and after. The pre-upgrade local backup was verified by restoration and remains under the ignored `.local-backups/` directory. No production database was accessed.

Regression coverage includes HTTP preview/apply persistence, rollback, malformed files, older and conflicting dates, changed baselines, source ownership, account locks, event-only and complete histories, API authority, pie allocation isolation, exact repeats, legacy nonfinite values, retained original records, clear/delete lifecycle and the standalone script. Node tests cover review acknowledgement, differences, failed apply and required dates.

Final verification: **217 Python tests and 21 subtests passed**, with **eight Node UI tests passing**. The local web app is running at schema 043. Read-only checks of the existing local Settings, import-history and TRIG holding paths succeeded; no local holdings were imported or rebuilt to exercise the feature.

Production rollout still requires its own recent backup/restore rehearsal and a bounded pause of all import/sync writers. Deploy the application and additive migration together. Retain the new tables during application rollback; pause imports/sync rather than reverting to an older writer that ignores source authority. No destructive downgrade is provided.
