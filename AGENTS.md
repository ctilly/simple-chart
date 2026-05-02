# SimpleChart — Agent Guidelines

## Project

Desktop stock charting application. Swing trading focus; no transaction management.
Inspired by TradStation and Webull for charting, specifically Anchored VWAP workflows.


## Domain vocabulary

- **AVWAP** — Anchored VWAP: volume-weighted average price anchored to a specific bar (UTC ms timestamp, never a bar index)
- **MAs are day-based** — a 50-day SMA means 50 trading days, converted to bar count per timeframe. The price value stays consistent across all timeframes.
- **`_kernel.py`** — pure numeric kernel file eligible for mypyc compilation. Lives inside its indicator's directory (e.g. `indicators/ema/_kernel.py`). Accepts and returns numpy arrays; no I/O.
- **session** — a trading day, not a user login session

## Engineering philosophy

- Build only what is actually needed right now. No speculative abstractions or "just in case" helpers, but build with the highest degree of professional quality.
- Question every dependency before adding it. Reach for the standard library first.
- Prefer simple, readable code over clever or defensive code.
- Do not add error handling for scenarios that cannot happen. Validate at system boundaries only.
- No docstrings, type hints, or comments on existing code that was not part of the current task; don’t add annotation churn outside the touched area.
- All new and modified functions must be fully typed (parameters, return values). Full typing is required for mypyc compatibility.

## Collaboration style

- After completing each logical unit of work (typically one module or one layer), stop and explain what was written and why before proceeding.
- Wait for review and explicit approval before moving to the next piece.
- Read files before proposing changes to them.
- Do not make unrequested changes — bug fix means fix the bug, not clean up the surrounding code.

## Indicator API

`simplechart.api` is the stable public import path for indicator authors. It
re-exports everything a plugin needs: `Indicator`, `ChoiceParam`,
`LINE_STYLE_OPTIONS`, `RENDER_CHART`, `SeriesFill`, `register`, `OHLCVSeries`,
`Bar`, `AnchorRecord`, `bars_for_n_days`, and `timestamp_ms_to_bar_index`.

Internal package paths (`indicators._base`, `indicators._registry`, etc.) are
not part of the public contract — they may change. External plugins must import
only from `simplechart.api`. Internal code (app layer, built-in indicators) may
import from internals directly.

## Building new indicators

A skill file is provided to guide you through building a new indicator correctly.
It covers the full process: reading the right reference files, deciding whether a
compiled kernel is needed, implementing each piece in the right order, and verifying
the result.

Find the skill for your agent:

| Agent  | Skill location                   |
|--------|----------------------------------|
| Claude | `.claude/skills/new-indicator.md` |
| Codex  | `.codex/skills/new-indicator.md`  |
| Gemini | `.gemini/skills/new-indicator.md` |
| Grok   | `.grok/skills/new-indicator.md`   |

Read the skill file before starting any indicator work.

