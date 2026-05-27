# SimpleChart — Agent Guidelines

For installation and environment setup, see `AGENT-SETUP.md`.
For step-by-step indicator building, see the skill files in `docs/skills/`.

## Project

Desktop stock charting application. Swing trading focus; no transaction management.  Includes popular swing trading indicators like the SMA/EMA, AVWAP, Pivot Points, Fibonacci Retracement, and RSI. 

## Stack

- Python 3.13+
- finplot (pyqtgraph) for charting
- yfinance for development data, Alpaca API for production
- SQLite for the local cache
- mypyc for hot numeric paths

## Layers

Data → Indicator engine → Charting (finplot) → Glue/orchestration

## Timeframes

5m, 15m, 30m, 39m, 65m, daily, weekly.
39m and 65m are non-standard and synthesized by aggregating smaller bars.

## Domain vocabulary

- **AVWAP** — Anchored VWAP: volume-weighted average price anchored to a
  specific bar (UTC ms timestamp, never a bar index).
- **MAs are day-based** — a 50-day SMA means 50 trading days, converted to bar
  count per timeframe. The price value stays consistent across all timeframes.
- **`_kernel.py`** — pure numeric kernel file eligible for mypyc compilation.
  Lives inside its indicator's directory (e.g. `indicators/ema/_kernel.py`).
  Accepts and returns numpy arrays; no I/O. Only reach for a kernel when the
  computation involves a tight loop where each value depends on the previous
  one (e.g. recurrence-relation smoothing). Simple vectorized math does not
  need one.
- **session** — a trading day, not a user login session.

## Engineering philosophy

- Build only what is actually needed right now. No speculative abstractions or
  "just in case" helpers, but build with the highest degree of professional
  quality.
- Question every dependency before adding it. Reach for the standard library
  first.
- Prefer simple, readable code over clever or defensive code.
- Do not add error handling for scenarios that cannot happen. Validate at
  system boundaries only.
- No docstrings, type hints, or comments on existing code that was not part of
  the current task; don't add annotation churn outside the touched area.
- All new and modified functions must be fully typed (parameters, return
  values). Full typing is required for mypyc compatibility.

## Collaboration style

- After completing each logical unit of work (typically one module or one
  layer), stop and explain what was written and why before proceeding.
- Wait for review and explicit approval before moving to the next piece.
- Read files before proposing changes to them.
- Do not make unrequested changes — bug fix means fix the bug, not clean up the
  surrounding code.

## Tooling

- git, GitHub, and source control are the user's responsibility; don't offer to manage this.

## External indicator libraries

During indicator research, use mature technical-analysis libraries as reference
material when useful, but do not treat them as automatic dependencies.

- Prefer formulas, behavior notes, and test vectors over adding runtime
  dependencies.
- Do not add a dependency without explicit user approval.
- Do not copy or adapt implementation code from another project without
  checking its license, documenting attribution requirements, and getting
  explicit user approval.
- If an external library is used only for validation, document that in the
  research summary and test plan.

## Extension API

`simplechart.api` is the stable public import path for extension authors
(indicators and tools). It re-exports everything a plugin needs:
`ChartExtension`, `ChoiceParam`, `LINE_STYLE_OPTIONS`, `RENDER_CHART`,
`SeriesFill`, `SeriesRender`, `HorizontalSegmentRender`, `VerticalLineRender`,
`MarkerRender`, `ChartExtensionRender`, `ChartEvent`, `ChartExtensionAction`,
`ChartExtensionMutation`, `ChartExtensionConfig`, `ChartExtensionAddMode`,
`HitTestResult`, `DragSession`, `DrawingSession`, `DrawingToolResult`,
`register_extension`, `register_store_handler`, `all_extensions`,
`get_extension`, `OHLCVSeries`, `Bar`, `ChartExtensionStoreRecord`,
`ChartExtensionStoreContext`, `ChartExtensionStoreHandler`, `DrawingStore`,
`AxisPolicy`, `bars_for_n_days`,
and `timestamp_ms_to_bar_index`. `ChartExtension` is the single base class for
both indicators and tools.

Internal package paths (`simplechart.extensions._base`,
`simplechart.extensions._registry`, etc.) are not part of the public
contract — they may change. External plugins must import only from
`simplechart.api`. Internal code (app layer, built-in indicators and tools)
may import from internals directly.

New extensions, including project indicators under `indicators/` and tools
under `tools/`, should import extension-facing APIs from `simplechart.api`
unless the task is explicitly to change framework internals.

## Where indicators live

- **Project indicators** — `indicators/`, as a `.py` file or a package directory
  with `__init__.py`. Use the dev install (see `AGENT-SETUP.md`).
- **Project tools** — interactive drawing tools (vertical line, Fibonacci
  retracement) live in `tools/`, as a package directory with `__init__.py`.
  Tools are chart extensions too; they load the same way indicators do.
- **User plugins** — `.py` files under `~/.simplechart/plugins/`. The normal
  install is sufficient when no compiled kernel is needed.

All locations load automatically on next launch — no manual controller
wiring required. The `register_extension()` call at the bottom of the
indicator file fires on import.

## Building new indicators and tools

Canonical skill files walk through the full process: reading the right
reference files, deciding whether a compiled kernel is needed, implementing
each piece in order, and verifying the result.

| Extension type | Skill location | When to use |
|----------------|----------------|-------------|
| Compute indicator | `docs/skills/compute-indicator.md` | Ordinary chart overlays or panel indicators that primarily convert OHLCV data into arrays (SMA, EMA, RSI, MACD). |
| Interactive indicator | `docs/skills/interactive-indicator.md` | Indicators with context-menu actions, drag handles, persistent drawing state, per-series configuration/removal, or custom render output (AVWAP). |
| Drawing tool | `docs/skills/drawing-tool.md` | Interactive overlays the user places and manipulates on-chart via the toolbar (vertical line, Fibonacci retracement), with `DrawingStore`-backed persistence (`AxisPolicy` per timeframe/session axis). |

Agent-specific skill directories (`.codex/skills`, `.claude/skills`,
`.gemini/skills`, and `.grok/skills`) contain forwarding files that point to
the canonical docs above.

Read the appropriate skill file before starting any indicator or tool work.
Before editing code, produce the spec requested by that skill file and
wait for approval.
