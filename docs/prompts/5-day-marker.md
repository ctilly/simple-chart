# Task: Build the 5-day trailing marker indicator

## Start here (do not skip)
You are the AI amalgamation of David M. Beazley, ArjanCodes, Tim Peters, and Will McGugan rolled into one; a Python legend.
You always write code with loose coupling, high cohesion, and modern software design principles and type annotations. Since this project uses Python 3.13, write all code using the most modern syntax Python 3.13 supports.

You are adding a new data-driven chart overlay indicator to the SimpleChart project. Before doing anything else:
1. Read `AGENTS.md` in full.
2. Read `docs/skills/compute-indicator.md` in full.
3. If the spec shows that normal compute-indicator output cannot render a vertical marker cleanly, stop and read `docs/skills/interactive-indicator.md` before proposing any boundary-crossing change.

## What the indicator must do
Build a 5-trading-day trailing vertical marker.

The marker is an indicator, not a drawing tool. The user does not place it, drag it, anchor it, or resize it. It is computed from the active symbol, timeframe, and loaded OHLCV bars. As new candles arrive or the user changes timeframe, the marker recomputes and moves programmatically.

The marker identifies the first candle included in the current/rightmost candle's 5-day moving-average window. It should match the existing day-based moving-average convention exactly:
- Compute `period = bars_for_n_days(5, series.timeframe)`.
- For the current/rightmost bar at index `i`, mark index `i - period + 1`.
- On the daily chart, `period = 5`, so the marker is on `current_index - 4`.
- On intraday charts, `period` is the number of candles that make up 5 regular trading sessions at that timeframe.
- During an active trading day, the marked intraday candle advances one candle at a time as each new candle appears.
- On the weekly chart, hide the marker because a 5-trading-day boundary is not meaningful at weekly resolution.
- The calculation must use the project's trading-session convention, not calendar days and not user login sessions.

Keep this indicator separate from the existing SMA indicator. The marker is closely associated with the 5-day SMA calculation, but it should not be folded into `SMAIndicator` and should not require a 5-day SMA line to be present.

Render the marker as a simple vertical line on the price chart. A purple dashed vertical line is acceptable unless the implementation spec proposes a better visual.

The marker must be configurable:
- color
- line width / weight
- line style
- visibility

Default behavior:
- The indicator is visible by default when the application opens.
- There is only one 5-day marker per symbol.
- Each symbol has its own independent marker state and configuration. Removing, disabling, or configuring the marker for `AAPL` must not affect `MSFT` or any other symbol.
- The same per-symbol marker state and configuration apply across that symbol's timeframes.
- The marker persists across application sessions indefinitely.
- Only the user can remove or disable it for that symbol.

## Non-negotiable constraints
- QUALITY: extremely high quality. Fully typed, mypyc-compatible where relevant, standard-library-first, no speculative abstractions, no dead code. Honor the project rules.
- INDICATOR SEMANTICS: do not model this as a user drawing. No start click, drag session, drawing commit, hit-test handle, or user-owned geometry unless the approved spec explicitly discovers that existing extension contracts require a limited interaction hook for configuration/removal.
- TOTAL ISOLATION: every part of this indicator lives inside its own package under `indicators/` unless a smallest-possible generic chart primitive or default-registration change is explicitly approved. No knowledge of the 5-day Marker may leak into unrelated app, chart, runtime, public API, or shared files. No `if extension_name == ...`, no series-key sniffing outside the package, and no 5-day Marker-specific chart branch.
- PUBLIC API: new extension code must import extension-facing APIs only from `simplechart.api`.
- BOUNDARY RULE: If any change outside the indicator package appears unavoidable, stop and follow the skill file's boundary side-task process: name the boundary, justify why an indicator-only change is insufficient, list files/functions, state behavioral risk, define focused tests, and wait for approval.

## Deliverables, in order — pause for my approval at each
1. Clarifying questions on the requirements above.
2. Research summary + citations + list of ambiguities.
3. Indicator spec using the compute-indicator skill's spec template. The spec must explicitly decide whether this remains a compute indicator or needs the interactive-indicator workflow because of custom vertical-line rendering, persistence, configuration, or removal semantics.
4. Boundary/file plan.
5. Implementation following the approved skill workflow and phase gates.

## Acceptance bar
- The marker appears by default for a loaded symbol.
- The marker is drawn on the first candle included in the current/rightmost candle's 5-day moving-average window.
- Daily and every supported intraday timeframe use the same `bars_for_n_days(5, timeframe)` convention as the existing moving-average indicators.
- The marker is hidden on weekly charts.
- The marker recomputes correctly after timeframe changes, symbol changes, and newly loaded bars.
- The user can configure color, line width, line style, and visibility.
- There is no user drag/reposition behavior.
- There is only one marker per symbol, shared across that symbol's timeframes.
- Each symbol's enabled/configured state is independent and persists across application restarts until the user removes or disables it for that symbol.
- Implementation-specific knowledge remains isolated to the indicator package except for approved generic framework/default-registration changes.
- Tests are added per the approved Verify step.
- `python -m pytest` runs clean.
- `python -m mypy app chart data simplechart tools indicators tests` runs clean.
- `python -m ruff check .` runs clean.
- `python -m compileall app chart data indicators simplechart tools tests` runs clean.
