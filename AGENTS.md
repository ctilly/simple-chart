# SimpleChart — Agent Guidelines

## Project

Desktop stock charting application. Swing trading focus; no transaction management.
Intended to replace TradeStation for charting, specifically Anchored VWAP workflows.


## Domain vocabulary

- **AVWAP** — Anchored VWAP: volume-weighted average price anchored to a specific bar (UTC ms timestamp, never a bar index)
- **MAs are day-based** — a 50-day SMA means 50 trading days, converted to bar count per timeframe. The price value stays consistent across all timeframes.
- **`_fast/` subpackage** — pure numeric kernels eligible for mypyc compilation. Can appear in any layer, not just indicators.
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

