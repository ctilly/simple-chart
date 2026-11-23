# Historical earnings and dividend markers

The user approved the two default-off Settings → UI toggles, Yahoo market-event data for either chart provider, and the `lxml` dependency. No credentials or provider routing changes are part of this slice. ETF witching dates remain a separate calendar feature.

## Behavior

- Saving Settings applies the toggles independently. New and existing profiles start with both off unless saved otherwise.
- Yahoo earnings rows with reported EPS and a date before today in New York supply historical earnings. Fetch one page of up to 25 rows. Yahoo's five-year dividend lookup supplies ex-dividend dates, including ETF distributions returned as dividends.
- Place markers above the first loaded intraday candle on the event's exchange-local date, the exact daily candle, or the covering weekly candle. Combine letters and counts when events share a candle. Only plot events covered by loaded bars.
- Price bars load independently. Reuse event dates on timeframe changes and realign them on history backfill. Selection or settings changes invalidate outstanding metadata results. Shutdown drains the running worker.

## Verification

- The live AAPL lookup failed with the missing HTML parser, then returned 25 New York–dated rows after installing `lxml` in a temporary target. `pyproject.toml` now declares the dependency for normal installs.
- Design QC confirmed that event markers must join active-marker cleanup and that Settings acceptance must trigger explicit refresh. Both changes are in the production path.
- `test_chart_event_settings_persist_and_update_markers_independently` failed at the missing Settings toggles and now verifies saved settings, marker redraw, daily/intraday/weekly placement, and independent removal. `test_late_historical_event_lookup_cannot_mark_new_symbol` verifies rejection during a real chart switch.
- Claude implementation QC found no blocking regressions. Same-day earnings are outside this slice's past-date rule. Marker alignment scans the loaded bars when a chart event is enabled, which is adequate for this slice's bounded event lists.
- Broader verification passed 139 tests. Ruff, scoped mypy, and diff checks passed. The active project Python environment has `lxml` 6.1.3 installed. Final acceptance is pending.
