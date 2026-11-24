# SimpleChart — Deferred Chart-Loading Work

These items were deliberately excluded from the completed chart-freeze and
Alpaca synthesized-cache corrections. None is approved for implementation.

## Market-data coordinator redesign

`MainWindow` still owns chart, Snapshot, Level 1, provider-validation, and
shutdown worker lifecycles. Revisit a coordinator only if another verified
defect spans several of those workflows or planned work would otherwise
duplicate scheduling and request-acceptance logic. Do not infer that the broad
coordinator design previously explored remains the right solution.

## Yahoo stale-cache policy

The current completed-interval and market-closed handling avoids the observed
short-tail Yahoo failures. If a properly aligned request still produces an
ambiguous no-price response, decide explicitly whether to display recent cached
bars with a visible stale warning. Never convert rate-limit, network, parsing,
or unexpected provider failures into silent success.

## PEP 561 `py.typed` packaging

`simplechart.api` has inline types, but a `simplechart/py.typed` marker alone
leaves re-exported `data` types as `Any` for installed consumers. Treat this as
a separate packaging/API unit: audit the public dependency closure, decide
which packages are marked, include markers in built artifacts, and verify a
wheel with an external strict-mypy consumer.

## Extended-hours and overnight session semantics

Alpaca currently downloads extended-hours bars but regular-session charts
filter them before caching/display. A future extended-hours option must include
session policy in cache identity. Exact-duration overnight futures candles also
need an explicit trading-session definition before changing current ET-date
grouping.

## Recent-first loading follow-ups (carried over 2026-11-06)

The recent-first loading project (first render of the visible window, chunked
background backfill, indicators rendering once their lookback is loaded) is
complete and user-accepted. Its working record was retired; these items remain.

- **Progressive AVWAP anchors on 39m/65m.** Synthesized bars are materialized
  from 5m only when the load finishes, so older anchors on those timeframes
  wait for completion. Native timeframes already show anchors as backfill
  reaches them. Fix would materialize 39m/65m incrementally as 5m chunks commit.
- **Provider connection reuse (F3).** Each chart load builds a new provider and
  TLS connection. Deferred until timing evidence shows the cost matters. If
  built: bounded idle client pool, create on demand, never block on the pool.
- **Candles without waiting for daily references.** The first render still
  joins the concurrent daily-reference fetch (needed for pivots and SMA daily
  warmup). Rendering candles first, then indicators, could shave ~0.5 s.
- **Animated loading overlay.** Experiment parked; the last review asked for
  revisions before it is reconsidered.
- **Real-session timing data.** Collect a few market-hours sessions with the
  opt-in load timing log (see README, "Chart-load timing log") before acting
  on any of the above.

## Completed infrastructure note

Snapshot/Level 1 route identity and responsive close-time draining are complete
and user-accepted as of 2026-11-06. They are no longer backlog items.

Claude collaboration uses the native Claude Code CLI directly. The tracked
`docs/claude-qc-integration.md` and `docs/skills/claude-collaboration.md` are the
current contract. Reopen it only for a verified compatibility or security
failure; do not repeat completed reviews.

## Intermittent test-suite crash: use-after-free in pyqtgraph (parked 2026-11-09)

Seen during error-sanitizer slice S1 (round 2) and not reproduced since.

- **Observed crash:** `RuntimeError: wrapped C/C++ object of type FinViewBox has been deleted`
  in `pyqtgraph InfiniteLine.boundingRect` (via `getViewBox().size()`), then
  `pure virtual method called` / segfault. It happened while pytest-qt processed events at the
  end of a test that built a `ChartWidget` and filled its three extension panels.
  A variant hit `YAxisItem.boundingRect` on a deleted `YAxisItem`. The module-scoped local test
  server had two handler threads alive at the time.
- **Conditions when it reproduced (S1 agent, 3/3):** `tests/test_chart_load_lifecycle.py`
  (which leaves about 640 closed MainWindow/QMenu widgets alive, because finplot keeps a global
  `windows` list), then `gc.collect()`, then the panel test.
- **Not reproduced on 2026-11-09** across 8 capped experiments:
  - the exact replay of the original test, imports, setup-hook GC and flags (GC collected 169
    objects, the same as the crash run);
  - the first 18 modules plus the panel test (498 objects, the same as the crash run);
  - the full suite with `gc.collect()` before every one of the 1,007 tests.
- **Hypothesis:** a Qt/Python lifetime race. A line item outlives the ViewBox it is attached
  to, and the trigger depends on destruction order and timing.
- **Ruled out as causes on their own:** the leftover windows; finplot's global window list; the
  extension-panel code.
- **Production impact (inferred, low):** the app keeps one long-lived chart window, and
  releasing a panel only hides it, so no ViewBox is destroyed.
- **2026-11-09 follow-up:** after S4 added 60 plugin-isolation tests (many MainWindows with extension
  panels, drawings and failures), the full suite (1284 tests) ran 3x sequentially, capped, with no
  crash. The S4 writer's own full runs also had no crash. After S5 (1338 tests), 3 more
  sequential capped full runs also had no crash.
- **Detector:** pytest's built-in faulthandler prints the Python stack on a crash. If it recurs,
  capture that trace with the exact test order. Then use gdb for the native stack and Qt
  `destroyed`-signal logging for item and ViewBox lifetimes.
- **Safety rule for any future investigation:** run test processes under a single cgroup with
  `systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 -p TasksMax=64` plus a
  timeout. Never name scratch scripts after stdlib modules (a scratch `bisect.py` on PYTHONPATH
  fork-bombed pytest and OOM-rebooted the machine twice). No recursive or automated bisection
  drivers.

## Second subscription data provider: Alpha Vantage (deferred 2026-11-09)

User wants to add Alpha Vantage later to test a new provider. Recommended scope: option (b),
generalize connection plumbing first, then add Alpha Vantage limited to free-tier daily and
weekly data.

- **Plumbing is still Alpaca-shaped (observed):**
  - fixed connections (`data/provider/config.py`: yfinance, alpaca-paper, alpaca-live);
  - `ProviderCredentials` is key-id + secret (Alpha Vantage uses one `apikey`);
  - `AlpacaConnectionDialog` in Settings;
  - about 7 `provider_name == "alpaca"` branches (application_settings, bar_comparison,
    cache, config);
  - hard-coded `_SOURCE_LABELS` in Data Quality.

  The fix is a per-provider declaration of credential fields, Settings form fields, feeds and
  cache namespace. Credential policy applies.
- **Alpha Vantage facts (docs read 2026-11-09):**
  - free tier: 25 requests/day;
  - `TIME_SERIES_INTRADAY` is premium-only (1/5/15/30/60min, one month per call via
    `month=YYYY-MM`, back to 2000-01);
  - `TIME_SERIES_DAILY` full history is premium (free: compact, last 100 bars);
  - `TIME_SERIES_WEEKLY` heading is not labeled premium;
  - realtime or delayed data needs premium plus an entitlement step;
  - `GLOBAL_QUOTE` takes one symbol per call; bulk quotes are premium;
  - no futures OHLC endpoint was seen;
  - premium tiers start at $49.99/mo for 75 requests/min (a second, 10x price list exists;
    which one applies to commercial use is unclear);
  - the API key is passed as the `apikey` query parameter;
  - the error and rate-limit response format and the intraday timestamp timezone were not
    found in the docs and must be verified empirically.
- **Error handling:** the new provider translates its errors into the neutral types in
  `data/provider/base.py` (error-sanitizer §5.9). `app/errors.py` needs no change.
