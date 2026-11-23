# Type a ticker from the chart

The user approved typing a ticker from the focused plot, including precedence over finplot's plain G, I, and F commands. The first letter, digit, or caret replaces the symbol field and transfers focus there. The field handles subsequent edits and Enter, using the existing symbol load path. Ctrl, Alt, and Meta combinations and non-text keys pass through. Drawing commit and cancellation retain their shortcuts. The handoff is inactive during a drawing or extension drag.

The price, volume, and extension panels use one graphics view, so the key handler lives on that view. The field cancels its deferred select-all when the chart hands it the first character. A chart result arriving during a focused edit preserves the draft. Submitting a ticker still normalizes it and triggers one chart load.

Codex and Claude design QC identified the focus timer and shortcut interactions. The production integration test failed at the missing focus handoff, then passed after the change. It clicks the plot, checks the modifier and active-drag guards, verifies the focused draft survives an in-flight timeframe load, and loads GOOG once on Return. Claude code QC found no regressions. The broader symbol-bar, chart-load, and plugin-window run passed 66 tests. Ruff and scoped mypy passed. All test and review processes exited.
