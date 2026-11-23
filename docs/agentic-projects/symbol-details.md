# Symbol details

The user approved implementation of a clickable, keyboard-accessible ticker and company name in the quote strip. Open one modeless window pinned to the clicked symbol. Opening another symbol retargets it. A chart switch leaves the old quote labeled as the last received quote.

## Scope

- Quote displays all Level 1 fields using the existing chart-route stream, plus bid–ask spread when both prices are present. No separate quote subscription.
- Yahoo profile and news run on demand and independently. Stocks show float and description. ETFs show description, fund family, benchmark, management fee when distinguished by the source, and expense ratio. Holdings show up to 20 rows supplied by the provider, ordered by weight. Unknown values and coverage are shown explicitly.
- Use verified issuer or index methodology for supported ETF rebalance information. Start with QQQ. Use published U.S. expiration calendars for witching dates and show unavailable beyond verified coverage. These dates appear in details only.
- Render provider strings as text. Open only validated HTTP(S) article links on user action. Provider failures remain local to their section.
- Bound concurrent work to one lookup per section. Drop results after close or retargeting, and drain workers before application shutdown. Use existing dependencies and no new credential or routing behavior.

## Verification

Through the real banner and MainWindow quote dispatch, check immediate quote display, clickable and keyboard entry, pinned quotes, ETF fields, independent failure, retargeting while a result is in flight, and safe shutdown. Run review gates before implementation and after passing tests. Final acceptance remains with the user.

## Evidence

- Red test failed at the missing banner link. The production integration test now clicks the banner, checks immediate route-qualified quote rendering, retargets during a delayed news lookup, verifies ETF fields, and confirms that close drains an active lookup.
- Live Yahoo data with the installed yfinance 1.2.0 supplied AAPL float and description and QQQ expense ratio and 10 holdings. Yahoo did not supply a separately identified QQQ management fee or a holdings as-of date. Rebalance schedules initially cover QQQ, and verified U.S. expiration dates cover 2026 and 2027.
- yfinance 1.2.0 used Yahoo's retired news endpoint. After user approval, the project floor was raised to 1.8.0 and the active environment was upgraded. SimpleChart's live adapter returned 10 headlines and 10 article links each for AAPL, QQQ, and NVDA. See the [upstream changelog](https://github.com/ranaroussi/yfinance/blob/main/CHANGELOG.rst).
- 112 regression tests passed before the dependency upgrade, and 24 focused tests passed afterward. Pip reported no broken requirements. Ruff, scoped mypy, and both Claude reviews passed. All verification and review processes exited.
