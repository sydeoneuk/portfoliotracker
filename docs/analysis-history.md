# Analysis market value and history

The market value card reads current holdings. Daily snapshots are recorded per
account and can be captured at different times or fail independently. Summing
only records present on a date incorrectly treated missing accounts as zero.
The local audit found one of three invested accounts on the latest history date.

The chart now builds account histories independently, carrying the last recorded
account value across gaps. Tooltips and the chart note disclose carried values;
dates before every selected account has an opening snapshot show the available
partial total. Tooltips show how many selected accounts have recorded values;
accounts without any recorded value are excluded rather than backfilled. Dates
with no recorded account values remain blank. Zero snapshots close an account's carried balance. Duplicate
account/pie/day rows use the latest capture and ID rather than double counting.
Unassigned historical account IDs are not guessed or merged into known accounts.

Outside-pie totals subtract pie values from each account before summing accounts.
Account, wrapper and pie scope queries remain owner-scoped. Recorded GBP totals
are converted to the card's display currency with the available current display
FX rate, explicitly disclosed for non-GBP views. Missing FX leaves a gap.

A separately labelled Current point uses the same complete market value and
display currency as the card, without another valuation query or snapshot write.
Unavailable current valuations never append a partial total. Historical views
omit the Current point and future records. Current and carried points have no
historical drilldown link because no single matching snapshot exists. Recorded
complete daily points retain date navigation.

Country/sector drilldowns show the current filtered point with a message that
historical totals for those filters are unavailable, rather than combining an
unfiltered history with a filtered current card.

No schema or financial-data changes are required. WSL Docker validation passed
326 Python tests and 43 subtests. Chart UI tests verify currency formatting,
recorded-date navigation, non-clickable current points and empty-state messaging.
The existing local database was read in a read-only transaction: Analysis returned
200 and the chart's Current point equalled the card to display precision.
