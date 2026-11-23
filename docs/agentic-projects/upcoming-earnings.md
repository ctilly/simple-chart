# Upcoming earnings and chart status layout

User approved the first slice, including Yahoo as the earnings source for charts served by Alpaca. Implementation, required design and code QC, and verification are complete. Final acceptance is pending.

## Scope

Place upcoming earnings immediately after the Level 1 quote fields in the top header using their typography and theme colors. Center the shortcut hints in the footer's available space, and place the chart data-source indicator at the far right beside the future market clock. Temporary status messages take the shortcut hints' place while active. Symbol entry, timeframe controls, and settings stay at the top.

Bring up ETF witching dates when discussing the next chart-event slice.

Use the existing Yahoo integration to look up only the selected symbol. Alpaca's [corporate-actions endpoint](https://docs.alpaca.markets/us/reference/corporateactions-1) includes cash and stock dividends and does not list earnings. Its suitability for historical dividend markers remains a separate decision.

Show a date or date range when supplied and a dash when data is unavailable. Identify Yahoo in the tooltip and avoid claiming issuer confirmation without evidence. Clear previous-symbol data on selection, reuse the outcome on timeframe changes, and prevent late results from appearing on a newer selection.

## Implementation and verification

1. Complete design QC under the repository's gated-autonomy workflow.
2. Write a production-path integration test that changes from A to B while A is still pending, releases A, and checks that chart loading and footer state are independent of the old request. Verify shutdown with chart and metadata work active.
3. Add the footer and scoped metadata worker with one active request and the latest pending selection. Include the worker in both existing shutdown gates. Validate the installed Yahoo adapter's timeout and date handling before relying on it.
4. Test date ranges, missing results, provider failure, repeated symbol selection, timeframe changes, and temporary status messages. Run focused checks, obtain code QC, and present the result for final acceptance.

The approved boundaries allow scoped app and Yahoo adapter changes, tests, live verification with public example symbols, and read-only review. Material behavior, dependency, credential, or external-data scope changes and final acceptance require user approval.

## Evidence

- The production entry point is `MainWindow._load`, reached by symbol entry and watchlist selection. The integration test `test_upcoming_earnings_discards_old_selection_and_drains_on_close` initially timed out waiting for the missing lookup. It now verifies chart progress, stale result rejection, timeframe reuse, and shutdown drainage.
- Claude design QC identified request identity, permanent status widgets, and shutdown drainage. Code QC confirmed these paths and suggested handling malformed upstream calendar values. The Yahoo adapter now treats an unexpected calendar shape as unavailable.
- The installed `yfinance` 1.2.0 calendar uses the library's bounded per-request timeouts. It exposes no per-call timeout argument or total deadline, and the lookup can involve multiple HTTP requests. The implementation uses the existing shared Yahoo client configuration.
- Live calendar checks returned dates for AAPL and MSFT and an empty result for SPY. This establishes sample coverage only. Automated tests stub the live Yahoo calendar and exercise replacement responses or a fake provider.
- The full offscreen suite passed with 1,419 tests and one skip after running with local socket access. Ruff and the scoped mypy check passed. Mypy with normal import following reports three unused ignores in the untouched Alpaca adapter.
