# Background jobs: shared lifecycle and exception boundary

Status: Approved (2026-11-23)
Version: 0.4
Owner objective: Make the app's newer background fetches resilient and
uniform, so future data features add a job instead of another hand-written
thread lifecycle.
Approval profile: Gated autonomy
Current graph phase: Accepted by the user, 2026-11-23
Last updated: 2026-11-23

## 1. Resumption state

```text
Last completed node: Codex design QC adjudicated; spec revised to 0.2
Active node: none
Active writer: none
Files owned by active writer: none
Pending join or gate: user final acceptance
Verified evidence available: §3 (fatal-exit probe)
Known unrelated workspace changes: docs/agentic-projects/handoff-2026-11-23.md (untracked, user's)
Exact next transition: on approval, Codex writes S1 RED tests
```

Roles for this project: Claude Code orchestrates, owns the specification and
adjudication, and performs code QC. Codex (`gpt-6.2-sol`, high effort) is the
single writer and gives a design QC. Claude has final say on findings.

Autonomy envelope:

```text
Approval profile: Gated autonomy
Approved objective: §2 and §4 goals (pending approval)
In-scope behavior and layers: app background jobs for search, details, and
  earnings; MainWindow close draining; the news timestamp in
  data/provider/symbol_details._published
Allowed writes: files listed per slice in §10, plus this artifact
Allowed verification: focused pytest offscreen, full suite once, ruff, mypy;
  loopback sockets for tests
Allowed external actions and data: Codex CLI receives repo paths and this
  spec; no credentials, private DBs, or live provider calls in tests
Prohibited actions: git or source control, dependency changes, edits outside
  §10, changes to the six controller workers, credential code
Mandatory user gates: spec approval, material amendments, final acceptance
Required reviewers: Claude code QC after each slice
Stop conditions: a test needs weakening; a change outside §10 is needed;
  two failed hypotheses on one defect
Reporting cadence: a short evidence update after each slice
```

## 2. User objective

Follow up the 2026-11-23 review of commit `c3eaf75`. Make the application
architecturally resilient to future growth. Fix the crash class found in the
review, and stop the per-feature duplication of thread lifecycle code.

## 3. Problem statement and observed behavior

- **Verified:** an exception that escapes `QThread.run` reaches
  `sys.excepthook`. In production `simplechart/output_safety._fatal_hook`
  calls `os._exit(1)`. A scratch probe that raised `KeyError` in `run`
  exited with status 1.
- **Observed:** the three background jobs added in `c3eaf75` have no outer
  `except Exception`:
  - `app/earnings_worker.EarningsWorker.run`
  - `app/symbol_details._DetailsThread.run`
  - `app/symbol_search._SearchThread.run`

  The older controller workers have one. `_Level1Worker.run` is an example
  (`app/controller.py` ~1416).
- **Observed:** the Yahoo functions guard only the network call. Their
  post-processing is unguarded:
  - `fetch_dividend_dates` reads `dividends.index`.
  - `fetch_past_earnings` does pandas filtering.
  - `search_symbols` iterates `.quotes`.
  - `fetch_profile` walks the ETF fields.
- **Inferred:** when Yahoo's response shape changes, an optional feature such
  as news, search, or earnings terminates the whole app.
- **Observed:** the three jobs share one lifecycle but use two threading
  idioms. The shared behavior:
  - the latest request wins;
  - at most one thread runs at a time;
  - one pending request is replaced on each new submit;
  - a cancellation flag is set on the running thread;
  - a generation counter rejects stale results;
  - a `busy` flag and an `idle` signal support close draining.
- **Observed:** earnings adds 8 fields and 6 methods to `MainWindow`.
  `_maybe_finalize_close` and `_finalize_close` repeat the same 7-term busy
  check. `_stop_data_workers` keeps a third hand-maintained list. Every new
  feature must edit all three.

## 4. Goals and non-goals

### Goals

1. One reusable latest-wins background job, `LatestJob`, with a built-in
   exception boundary.
2. Symbol search, symbol details (profile and news), and earnings (upcoming
   and historical events) run on `LatestJob`. None of them can terminate
   the app.
3. Earnings state and lifecycle move out of `MainWindow` into a component.
4. `MainWindow` drains and stops these components through one list. There is
   one busy check, used by both close paths.

5. (Amendment 0.3, user decision Q1) All reference data goes through the
   `DataProvider` interface, so any data source can supply it. Alpha Vantage
   is only an illustrative example; no new source is planned. Reference data here means profile, news, symbol search, and
   upcoming and historical earnings and dividends. See §6.4.

### Non-goals

- Chart load, older history, and live refresh workers. Their acceptance and
  cleanup rules really differ.
- The Level 1, watchlist snapshot, and provider validation workers. These
  move later, only when a change touches them.
- Caching reference data, rate limiting, or a new dependency.
- Choosing which source serves reference data per provider or per feature,
  and fallback chains. Yahoo stays the only reference source. That routing
  decision belongs to the pluggable-providers project (§12 Q1).
- The review's smaller findings:
  - the `statusBar()` override;
  - the hard-coded highlight color;
  - inline settings keys;
  - the fund calendar ending in 2027;
  - the yfinance version cap.

  These are tracked in §12 Q2 and not fixed here.
- Any user-visible behavior change beyond §5.

## 5. User-visible behavior

Unchanged, except:

- An unexpected exception inside one of these jobs now shows that feature's
  existing "unavailable" state, instead of killing the app:

  | Feature | Unavailable state |
  |---|---|
  | Search | No suggestions |
  | Profile | "Yahoo Finance profile unavailable." |
  | News | "Yahoo Finance news unavailable." |
  | Upcoming earnings | Header keeps "—" |
  | Historical events | No markers |

  The failure is logged once through `app.errors.report_error`: ERROR, or
  WARNING for `UserFacingError`. The log contains only `describe()` facts,
  never raw response content.
- Internal timing: upcoming earnings and historical events become two
  independent jobs.
  - A new symbol's upcoming date no longer waits behind the previous
    symbol's blocked history fetch.
  - Toggling the E/D settings refetches only historical events, not the
    upcoming date.
  - The two Yahoo calls may now overlap.
- A news item whose timestamp can't be shown in New York time (for example,
  year 1) is listed without a time instead of crashing the GUI (QC-01).

## 6. Design

### 6.1 `app/background_job.py`: `LatestJob(QObject)`

```python
class LatestJob(QObject):
    succeeded = pyqtSignal(object)  # result of the current request only
    failed = pyqtSignal()           # unexpected exception in the current request only
    idle = pyqtSignal()             # no thread running and nothing pending

    def __init__(self, action: str, parent: QObject | None = None) -> None
    @property
    def busy(self) -> bool
    def submit(self, work: Callable[[threading.Event], object]) -> None
    def cancel(self) -> None
```

Rules:

- **submit:** increments the generation and sets the running thread's
  cancellation `Event`. It replaces any pending work, then starts the work
  immediately if no thread is running.
- **cancel:** increments the generation, drops pending work, and sets the
  running `Event`. No `succeeded` or `failed` follows for cancelled work.
- **The thread:** a `QThread` subclass. `run` calls `work(event)` inside
  `try/except Exception` and stores the outcome (value or failure) on the
  thread object. It emits nothing itself.
  - On an exception it calls
    `report_error(_LOGGER, exc, ErrorContext(action))`, unless the event is
    already set.
- **Single GUI delivery point (QC-02):** the job's handler for the thread's
  `finished` signal does all delivery, in this order:
  1. Clear the running-thread reference, so `busy` reflects only pending
     work.
  2. Call `deleteLater` on the thread.
  3. Emit `succeeded` or `failed`, only if the stored generation still
     matches and the event is not set.
  4. Start pending work, if any.
  5. Emit `idle` if nothing is running.

  A result handler may call `submit` or `cancel` re-entrantly; tests cover
  this. `idle` fires on drain even after `cancel`, because close relies on
  it.
- **Typing:** `work` receives the `Event` so it can stop between network
  calls. The result is typed `object`; callers narrow it. A generic
  `QObject` subclass conflicts with sip's metaclass, so no `Generic[T]`.

### 6.2 Ports

- **`app/symbol_search.SymbolSearch`:** keeps its public API (`results`,
  `idle`, `busy`, `edit`, `cancel`) and the 250 ms debounce. It runs on one
  `LatestJob`. `failed` means no suggestions. `_SearchThread` is removed.
- **`app/symbol_details.SymbolDetails`:** keeps its public API (`idle`,
  `busy`, `show`, `update_quote`, `retain_quote`, `stop`). It uses two jobs,
  profile and news.
  - `busy` is true when either job is busy.
  - `idle` is emitted when both are idle.
  - `failed` maps to `set_profile(None)` and `set_news(None)`.
  - `_DetailsThread` is removed.
- **Earnings:** `app/earnings_worker.py` becomes `app/earnings.py` and
  defines `HistoricalEventDates` and an `EarningsFeed(QObject)` component.
  - It owns the symbol, visibility, historical dates, and two `LatestJob`s:
    upcoming and history.
  - Keys (QC-05): upcoming is keyed by symbol, history by symbol plus
    visibility. `select` with an unchanged key, such as a timeframe change,
    starts nothing. A symbol change resets history to `((), ())` and the
    header to "—". With both toggles off, no history job runs. Both jobs
    keep using the Yahoo route's aggregator.
  - Signals: `upcoming_changed(str, bool)` carries the label and highlight;
    `history_changed()`.
  - API: `select(symbol, visibility)`, `stop()`, `busy`, `idle`, and
    read-only `symbol`, `visibility`, and `history`.
  - The label and the 0–5-day highlight rule move into a pure function in
    the same module.
  - `chart_events.py` updates its import.
  - `MainWindow._refresh_chart_event_markers` stays in `MainWindow`, because
    it needs the current series, the load-pending state, and the plot
    manager. It reads its data from the feed.
  - `upcoming_changed` connects through a `MainWindow` slot that calls
    `set_earnings_date(label, highlight=highlight)`. A direct connection
    silently drops the keyword-only highlight (QC-06).
- **News timestamps (QC-01, revised by S4-QC-04):**
  - The contract: `NewsArticle.published` is a timezone-aware datetime in
    any zone, or `None`.
  - `app/symbol_details` converts each item to `MARKET_TIMEZONE` through one
    helper. An item whose conversion raises `OverflowError` or `ValueError`
    is shown without a time.
  - Safety lives in the app once, not in each provider's parser.
- **Test seams:** jobs look up the module-level fetch functions at call time.
  The existing `monkeypatch.setattr` seams in `tests/test_symbol_refresh.py`
  keep working. These are `symbol_search_module.search_symbols`,
  `symbol_details_module.fetch_news` and `fetch_profile`, and
  `provider.fetch_*`.

### 6.3 `MainWindow` draining

- `MainWindow` keeps one tuple of background components: symbol details,
  the symbol bar's search, and the earnings feed. The search is exposed
  read-only as `SymbolBar.search`.
- Each component satisfies a small `Protocol` with `busy`, `idle`, and
  `stop()`. `SymbolSearch` gains `stop()`, which calls `cancel()` (QC-07).
- Each component's `idle` signal connects to `_maybe_finalize_close`.
- `_stop_data_workers` calls `stop()` on each component. It still calls
  `SymbolBar.stop_search()` so the popup closes immediately (QC-07).
- A single `_data_workers_busy()` method covers both the existing thread
  fields and `any(component.busy ...)`. Both close paths use it.
- The 8 `_earnings_*` fields and `_on_earnings_finished` are removed.

### 6.4 Reference-data interface (S4, amendment 0.3)

The existing `DataProvider` (`data/provider/base.py`) is the interface. It
already carries `fetch_company_name` and the three earnings and dividend
methods, each with a neutral default. One abstraction serves both market
and reference data. A provider that lacks a capability keeps the default
(`None` or `()`). A separate interface would duplicate the route,
connection, and credential machinery.

- **New methods on `DataProvider`:**
  - `fetch_profile(symbol) -> SymbolProfile | None`
  - `fetch_news(symbol) -> tuple[NewsArticle, ...] | None`
  - `search_symbols(query) -> tuple[SymbolMatch, ...]`

  Each has a neutral default, and `Aggregator` gets matching pass-throughs.
- **Models:** `SymbolProfile`, `Holding`, `NewsArticle`, and `SymbolMatch`
  move to `data/models.py`.
- **Yahoo implementation:** the code in `data/provider/symbol_details.py`
  and `data/provider/symbol_search.py` moves into `YFinanceProvider`, or
  into a private helper module that only `YFinanceProvider` imports. Both
  old modules are deleted. QC-01's `_published` fix moves with it.
- **App wiring:** `MainWindow` holds one `_reference_aggregator`, which is
  `self._yahoo_route.aggregator` today. This is the single place that
  decides the reference source for these six jobs. The company name in the
  Level 1 header keeps its existing `_resolve_company_name` path and cache.
  Name precedence and cache identity across sources belong to the
  pluggable-providers project (S4-QC-03).
- **Aggregator forwards:** kept as thin forwards, matching the existing
  earnings path, so S4 doesn't change route construction. Whether to inject
  providers directly is revisited in the pluggable-providers project
  (S4-QC-02).
  - `SymbolBar` → `SymbolSearch`, `SymbolDetails`, and `EarningsFeed`
    receive it through their constructors.
  - Jobs call its methods. Nothing under `app/` imports yfinance or a
    concrete provider module for reference data.
- **Tests:** the seams move from patching module functions to patching
  provider methods. The earnings tests already patch provider methods.
  Only seam lines change; asserted behavior stays the same.



| Feature | Production path |
|---|---|
| Search | `SymbolBar._on_search_edit` → `SymbolSearch.edit` → `LatestJob.submit` |
| Details | `AppHeader.details_requested` → `MainWindow._open_symbol_details` → `SymbolDetails.show` → two `LatestJob.submit` calls |
| Earnings | `MainWindow._load` and the settings-accepted path → `EarningsFeed.select` → jobs → `AppHeader.set_earnings_date` and `_refresh_chart_event_markers` |
| Close | `MainWindow.closeEvent` → `_stop_data_workers` → `_maybe_finalize_close` / `_finalize_close` → `_data_workers_busy()` |

## 8. Acceptance criteria

- **AC1:** for each of search, profile, news, upcoming earnings, past
  earnings, and dividends, an exception raised by the fetch function through
  the production path:
  - leaves the app running;
  - shows that feature's unavailable state (§5);
  - logs exactly one record at the `report_error` severity;
  - keeps an ordinary exception's synthetic message text out of the log
    (QC-08).

  A year-1 news timestamp renders the headline without a time (QC-01).
- **AC2:** latest-wins, stale-result rejection, and close draining are
  unchanged for all three features. The existing tests below pass. Allowed
  edits are import paths, plus the bodies of four tests that reach removed
  `MainWindow` internals (QC-04):
  - the settings and stale-history tests wait for the feed to drain instead
    of `_earnings_thread`;
  - the upcoming close test no longer reads `_earnings_pending`;
  - the highlight test drives a fake provider and a fixed clock instead of
    calling `_on_earnings_result`.

  The asserted behavior must not weaken. Declare every edit in the report.
  - `test_symbol_box_search_selects_a_listing_and_keeps_direct_ticker_entry`
  - `test_symbol_details_retargets_without_showing_late_news`
  - `test_chart_event_settings_persist_and_update_markers_independently`
  - `test_late_historical_event_lookup_cannot_mark_new_symbol`
  - `test_upcoming_earnings_discards_old_selection_and_drains_on_close`
  - `test_upcoming_earnings_highlights_dates_within_five_days`
  - `test_shutdown_leaves_no_refresh_running`
- **AC3:** closing while any component job is in flight finalizes only after
  every job drains. Both close paths use the single busy check.
- **AC4:** `MainWindow` has no `_earnings_*` thread, worker, event,
  generation, or pending fields.
- **AC5:** `LatestJob` behavior is proven by unit tests:
  - the latest submit wins;
  - a pending submit is replaced;
  - a running job's event is set on submit and on cancel;
  - nothing is emitted after cancel;
  - an exception becomes `failed` plus one log record;
  - an exception after cancel is not logged;
  - `idle` fires once when the job drains, including after cancel;
  - a `succeeded` handler that re-submits leaves the job busy, with no
    stray `idle`.
- **AC7 (QC-05, QC-07):**
  - with an old symbol's history job blocked, a new symbol's upcoming date
    still appears and no stale markers are drawn;
  - settings edits make zero upcoming calls;
  - closing with all five jobs held closes the search popup immediately and
    finalizes only after the last job is released.
- **AC8 (S4):**
  - a fake `DataProvider` passed as the reference source drives all six
    capabilities through the production paths, with no network access;
  - the market-route fake is a separate object whose reference methods fail
    the test if called (S4-QC-05);
  - the reference fake returns UTC news timestamps, which render as ET, and
    a year-1 timestamp, which renders without a time (S4-QC-04);
  - a test scans `app/` and fails if it imports `yfinance` or
    `data.provider.yfinance_provider`, `symbol_details`, or `symbol_search`;
  - AC1, AC2, and AC7 stay green.
- **AC6:** ruff and mypy are clean on touched files, and the full offscreen
  suite passes.

## 9. TDD test matrix

| Test | File | Fails before because |
|---|---|---|
| `LatestJob` behavior (AC5) | new `tests/test_background_job.py` | the module does not exist |
| Raising search keeps the app alive and shows no suggestions | `tests/test_symbol_refresh.py` | pytest-qt reports the unhandled exception in `QThread.run` |
| Raising profile or news shows the unavailable text | `tests/test_symbol_refresh.py` | same |
| Raising upcoming or past earnings or dividends leaves "—" or no markers, and close still finalizes | `tests/test_symbol_refresh.py` | same |
| `MainWindow` close with jobs in flight in all three components | existing plus one new assertion | — (regression guard) |

RED mechanism (QC-03, verified by a Codex probe): wrap the action and the
drain in `qtbot.captureExceptions()`, then assert the capture is empty after
it exits. Otherwise pytest-qt restores `sys.excepthook` mid-failure, and the
conftest hook-identity check adds a teardown error. Use synthetic exception
text.

## 10. Implementation slices

Codex is the single writer for every slice. Claude does code QC after each
slice.

| Slice | Writes | Exit |
|---|---|---|
| S1 `LatestJob` | `app/background_job.py`, `tests/test_background_job.py` | RED then GREEN; ruff and mypy clean |
| S2 Search and details port | `app/symbol_search.py`, `app/symbol_details.py`, `tests/test_symbol_refresh.py` (new failure tests only) | AC1 (search, profile, news), AC2 tests green |
| S3 Earnings feed and draining | `app/earnings_worker.py` → `app/earnings.py`, `app/chart_events.py` (import), `app/controller.py`, `app/symbol_bar.py` (`search` property only), `app/symbol_search.py` (`stop()`), `tests/test_symbol_refresh.py` | AC1 (earnings), AC2–AC4, AC7 |
| S4 Reference-data interface | `data/provider/base.py`, `data/provider/yfinance_provider.py` (plus an optional private helper module), delete `data/provider/symbol_details.py` and `data/provider/symbol_search.py`, `data/models.py`, `data/aggregator.py`, `app/symbol_search.py`, `app/symbol_details.py`, `app/symbol_bar.py`, `app/earnings.py`, `app/controller.py` (wiring only), tests (seam lines plus the AC8 tests), `tests/test_yfinance_provider.py` (moved provider tests) | AC8; AC1, AC2, and AC7 still green |
| V Verification | none | full offscreen suite once, ruff, mypy (AC6) |

## 11. Risks and falsification checks

- **Re-entrancy (QC-02):** resolved by the single `finished` delivery
  point. Check: the re-submitting and cancelling handlers in AC5.
- **Deferred deletion:** `deleteLater` on a finished `QThread` must not run
  while a pending start reuses state. Each run gets a fresh thread object.
  Check: a stress-style unit test with three rapid submits.
- **Earnings overlap:** splitting earnings lets upcoming and history
  overlap. Check: AC7 and the revised stale-history test.
- **Close-time cancellation:** a cancelled job blocked in a network call
  delays close until Yahoo's timeout, as it does today. Not changed here.

## 12. Open questions for the user

- **Q1 (resolved 2026-11-23):** the user chose to build the reference-data
  interface now. The goal: any data source can be plugged in and work with
  SimpleChart. Alpha Vantage is an example, not a planned addition. The
  interface is slice S4 (§6.4).
  - Routing model (user decision, 2026-11-23): sources may be mixed. The
    selected provider serves each capability it supports, and Yahoo fills in
    what it lacks (for example, Alpaca bars with Yahoo company data). S4
    keeps Yahoo for all six reference jobs, which matches this rule today.
    Per-capability routing is built in the follow-on pluggable-providers
    project.
- **Q2 (recommend a separate small batch):** the smaller review findings.
  - `MainWindow.statusBar()` override
  - Hard-coded `#ffdc58` earnings highlight that bypasses theme tokens
  - Inline `show_past_earnings` / `show_dividends` keys
  - `fund_calendar` dates end in 2027
  - No upper cap on yfinance; confirm that `lxml` was approved

## 13. QC findings and dispositions

Codex design QC (`gpt-6.2-sol`, high effort), 2026-11-23. Adjudicated by
Claude.

| ID | Sev | Finding | Disposition |
|---|---|---|---|
| QC-01 | High | A year-1 news timestamp overflows in `set_news` on the GUI thread and exits the app (Codex verified) | Accepted: convert at the provider boundary; test it (§6.2, AC1) |
| QC-02 | Med | A nested event loop can deliver `finished` before a result handler returns | Accepted: store the outcome and deliver once from the `finished` handler (§6.1) |
| QC-03 | Med | RED needs `qtbot.captureExceptions()` to avoid a teardown error | Accepted (§9) |
| QC-04 | Med | Four existing tests reach removed internals, so AC2's import-only rule conflicts with AC4 | Accepted: bounded test-body edits; behavior not weakened (AC2) |
| QC-05 | Med | Split earnings needs explicit keys, reset, and skip rules | Accepted (§6.2, AC7) |
| QC-06 | Med | A direct signal to `set_earnings_date` drops the keyword-only highlight | Accepted: adapter slot (§6.2) |
| QC-07 | Med | Close tuple: search has `cancel()`, not `stop()`; popup dismissal; idle after cancel | Accepted: Protocol plus `SymbolSearch.stop()`; keep the `stop_search()` call (§6.3, AC5, AC7) |
| QC-08 | Low | `report_error` logs `UserFacingError` at WARNING, not ERROR | Accepted (§5, AC1) |

S4 design QC (Codex), 2026-11-23:

| ID | Sev | Finding | Disposition |
|---|---|---|---|
| S4-QC-01 | Low | Optional methods on `DataProvider` are fine; a narrower interface is possible | Accepted: keep optional methods |
| S4-QC-02 | Low | Aggregator forwards are a pure façade | Accepted: keep for S4; revisit in pluggable providers |
| S4-QC-03 | Med | The "single place" claim excludes company-name routing | Accepted: scoped to the six jobs (§6.4) |
| S4-QC-04 | Med | The news timestamp contract is Yahoo-specific; a UTC source would be mislabeled as ET | Accepted: any-zone contract, with safe conversion in the app (§6.2) |
| S4-QC-05 | Med | One shared fake can't prove source selection | Accepted: separate market and reference fakes (AC8) |

Codex noted that `docs/credential-security.md` is missing from the checkout.
It was deleted in `baf4eea`, and AGENTS.md requires it; reported to the
user. S4 doesn't change credential handling.

Codex's account of `c3eaf75`:
- Deferring the extraction was deliberate.
- The mixed threading idioms and the missing outer exception boundary were
  oversights. The fatal-exit path was not considered.

## 14. Plan-change log

- 0.1: initial draft.
- 0.2: accepted Codex design QC-01 to QC-08 (§13).
- S1 complete, 2026-11-23. Codex wrote `app/background_job.py` and
  `tests/test_background_job.py`: 8 tests, RED on the missing module, GREEN
  5 out of 5. Claude QC:
  - reran the tests 3 times: green;
  - ruff and mypy clean;
  - a mutation that removes the latest-thread idle rule fails only the
    nested-loop test.

  No findings.
- Q2 small fixes (user said "fix now"), run between S1 and S2:
  - theme token for the earnings highlight;
  - settings-key constants;
  - computed witching and QQQ dates;
  - `yfinance<2`.

  The `statusBar()` override item was dropped by Claude. The footer must sit
  inside the themed frame, which `setStatusBar` can't do. Removing the
  override means about 35 call-site edits (15 in the app, 20 in tests) with
  no change in behavior.
- Small-fixes batch complete (Codex, medium effort). Claude QC:
  - Easter computus checked against known dates for 2008–2285;
  - affected tests rerun: green;
  - the holiday-window reasoning holds.

  No findings.
- S2 complete (Codex). It added 4 production-path tests: search, profile,
  and news failures, plus news timestamps. RED came from the escaped
  exceptions and the year-1 `OverflowError`; GREEN passed 3 out of 3 times.
  Claude QC: reran 19 tests, green; ruff clean. mypy on the app files shows
  3 `unused-ignore` errors in the untouched `data/provider/alpaca_provider.py`,
  likely pre-existing and environment-dependent; to be confirmed at V.
  - QC-S2-1 (Med): `SymbolDetails._on_profile` and `_on_news` use `assert`
    for type narrowing inside Qt slots. A failed assert there hits
    `_fatal_hook`, the same crash class this project removes, and S4's
    future providers could trip it (for example, a list instead of a
    tuple). Disposition: replace with `typing.cast`. Folded into S3,
    because S3 owns the adjacent files.
- S3 complete (Codex). `EarningsFeed` lives in `app/earnings.py`, and
  `app/earnings_worker.py` is deleted. A `_BackgroundComponent` Protocol
  plus `_data_workers_busy()` cover both close paths. There are 7 new tests,
  each RED for its intended reason, and the four allowed test-body edits are
  declared. Claude QC:
  - reran 127 tests across 5 files: green;
  - ruff clean; mypy shows only the 3 `alpaca_provider.py` unused-ignores
    (the file is unmodified, so they are environmental);
  - removed test lines checked: each assertion was moved or replaced, not
    dropped.
  - Nit QC-S3-1: `SymbolBar.search_busy` is now unused. Remove it in S4,
    which owns `symbol_bar.py`.
- S4 internal adjustment: Codex paused before writing anything, because two
  tests sit outside S4's write list. `tests/test_fund_calendar.py` imports
  `SymbolProfile` from the module being deleted. `tests/test_symbol_bar.py`
  constructs `SymbolBar()` with no arguments. Claude authorized seam-only
  edits to both: the import path and the constructor argument, with
  assertions unchanged.
- S4 complete (Codex). Reference data runs through `DataProvider`: new
  optional `fetch_profile`, `fetch_news`, and `search_symbols` methods, with
  the models in `data/models.py`. The Yahoo code moved into
  `YFinanceProvider`, and `data/provider/symbol_details.py` and
  `symbol_search.py` are deleted. `MainWindow._reference_aggregator` feeds
  `SymbolBar`/`SymbolSearch`, `SymbolDetails`, and `EarningsFeed`. New
  tests: the AC8 routing test with distinct market and reference fakes; UTC
  and year-1 news; and the `tests/test_reference_import_boundary.py` AST
  scan, which was RED on 7 imports. Existing tests: seam lines only.
  Claude QC: QC-S2-1 is fixed with `cast`, and QC-S3-1 is fixed. The
  boundary scan covers `import x`, `import x as y`, and `from x import y`.
  No credential changes.
- V, 2026-11-23 (Claude). Full offscreen suite: 1411 passed, 1 skipped,
  11 failed, 36 errors. All failures are in `test_provider_errors.py`,
  `test_error_display_sites.py`, and `test_yahoo_history_integration.py`,
  with `AttributeError: module 'yfinance' has no attribute 'shared'`.
  - **Verified unrelated:** the same three files at an exported copy of
    HEAD `c3eaf75` give the same result, 11 failed and 36 errors.
  - Cause: the test helpers (`tests/realistic_errors.py:478` and
    `test_yahoo_history_integration.py:105`) patch `yfinance.shared`, which
    yfinance 1.8.0, installed in `c3eaf75`, still ships but never imports.
  - Reported, not fixed.

  ruff (whole repo): clean. mypy (`app`, `data`, `chart`, `simplechart`):
  only the 3 pre-existing `unused-ignore` errors in the unmodified
  `data/provider/alpaca_provider.py`.
- Production call sites:
  - `MainWindow.__init__` builds `EarningsFeed`, `SymbolDetails`, and
    `SymbolBar` with `_reference_aggregator`.
  - `MainWindow._load` and the settings-accepted path call `_earnings.select`.
  - `MainWindow.closeEvent` → `_stop_data_workers` → `_data_workers_busy`.

  Integration tests: the AC1, AC7, and AC8 tests in
  `tests/test_symbol_refresh.py`.
- Repo hygiene (user request): AGENTS.md now has a rule that temporary files
  live in `docs/agentic-projects/`. `tmp/chart-loading-deferred-backlog.md`
  and `tmp/simulate_split.py` moved there.
- 0.4.1 (internal adjustment, S1): Codex verified with a probe that a
  `succeeded` handler running a nested event loop leads to two `idle`
  emissions. The submitted replacement drains inside the loop, then the
  outer handler emits a second `idle`. Rule added to §6.1 step 5: `idle` is
  emitted only by the finished handler of the most recently started thread.
  This keeps AC5's "once per drain". Covered by a nested-loop unit test.
- 0.4: accepted S4-QC-01 to S4-QC-05; QC-01's fix moves from the provider
  parser to the app.
- 0.3: material amendment. Adds goal 5, §6.4, AC8, and slice S4, after the
  user's Q1 decision. The Codex design QC of S4 is adjudicated before S4
  starts. A material finding returns to the user.
- Accepted by the user, 2026-11-23. Follow-up, outside this project's
  scope: the 47 yfinance test failures. Two test fixes:
  - `tests/realistic_errors.py` and `tests/test_yahoo_history_integration.py`
    patched `yfinance.shared`, which yfinance 1.8.0 never imports. It keeps
    download state per call in `multi._DownloadCtx`. The dead patch loops are
    removed.
  - With that fixed, two `EMPTY` cases failed on `KeyError: 'tradingPeriods'`.
    yfinance 1.8 reads it on intraday requests without pre/post bars. The
    synthetic empty payload now includes it, as real Yahoo intraday
    responses do.
