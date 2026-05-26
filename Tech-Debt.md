# SimpleChart — Technical Debt Triage

_Whole-codebase review of all layers (data, indicator engine, charting, app/glue),
tests, build config, and a mypy/pytest baseline._

## Baseline health
- ✅ `pytest`: **54 passed**
- ❌ `mypy .` (project declares `strict = true`): **51 errors across 10 files**
- Duplicate trees and a committed `.so`/`.db` exist locally but are correctly
  **gitignored** — not committed. `__.git-backup`, `notes/`, caches all ignored
  properly. No stray artifacts in git.

---

## 🔴 High — architecture / standards

### 1. Two unfinished refactors left behind re-export shims that only tests keep alive
- `indicators/_base.py`, `_registry.py`, `_store_registry.py` are now 3-line
  `from simplechart.extensions._X import *` shims. The real code is in
  `simplechart/extensions/`.
- `indicators/vertical_line/` and `indicators/fib_retracement/` are likewise shims
  that re-export `tools/...`. The real code moved to `tools/`.
- Nothing in the app imports these shims — only tests do, and several tests exist
  *solely* to assert the shims alias correctly (`test_*_legacy_*_aliases_*`).
- `plugins.py` loads **both** `indicators` and `tools` packages, so the shims are
  imported at runtime too (harmless re-registration, but live).
- **Decision needed:** complete the move (delete shims + their alias-only tests,
  update `AGENTS.md` which still says tools live in `indicators/`), or commit to
  keeping them as a public-compat layer. Right now it's neither.

### 2. `Indicator` → `ChartExtension` rename is half-done; ~25 back-compat aliases litter every module
- `_base.py` ends with 8 `Indicator* = ChartExtension*` aliases; `_registry.py` has
  `register`/`register_extension`/`register_indicator` + `get`/`get_extension`/
  `get_indicator` + `all_*`; `indicator_runtime.py`, `indicator_store.py`, `state.py`
  each tail with more aliases.
- Dual vocabulary is confusing: classes are named `ChartExtension*` but their fields
  are still `indicator_name`, and the public API doc table in `api.py` lists only the
  `Indicator*` names while `__all__` exports both sets.
- **Decision needed:** pick one vocabulary, delete the other, drop the
  alias-verification tests.

### 3. The codebase does not pass its own declared strict-mypy gate (51 errors)
AGENTS.md mandates full typing "required for mypyc compatibility," yet:
- `app/indicator_config.py` (11): `_build_field` reuses one inferred-`QCheckBox`
  variable for `QSpinBox`/`QComboBox`/`QLineEdit` — a real type-soundness gap, easily
  fixed with `w: QWidget`.
- `chart/plot_manager.py` (14): accesses `.vb`/`.addItem`/`.items` on `object`-typed
  handles (the `_ViewBoxLike` protocol from `viewport.py` isn't used here).
- `yfinance_provider.py` (10), `watchlist.py` (7), `legend.py` (4), plus redundant
  casts in `indicator_store.py`/`indicator_runtime.py`.

---

## 🟠 Medium — dead code, duplication, fragility

### 4. Dead code
- `PlotManager.clamp_initial_zoom()` — ~30 lines (incl. detailed docstring) —
  **never called**.
- `ChartExtensionRuntime.hit_test()` — never called; superseded by
  `drawing_hit_test()`/`begin_drag()`.
- `is_avwap_series_key()` — referenced only by tests.
- Aliases `IndicatorRenderPass` / `IndicatorRemoval` — never referenced.

### 5. Speculative unimplemented feature: `SeriesFill` / `series_fills()`
Exported from the public API and documented, but `series_fills()` is never called and
`PlotManager` has no fill rendering — its own docstring admits "support… is planned but
not yet implemented." Directly contradicts the "build only what's needed now, no
speculative abstractions" philosophy.

### 6. ~80 lines of near-duplicated render logic in `controller.py`
Across `_draw_indicator_render`, `_draw_drag_render`, and `_draw_preview_render` — the
series/segment/vertical-line/marker draw loops are copy-pasted with small variations.

### 7. `_ref_` substring is an overloaded implicit contract
It means "gray dashed reference styling" in `render_from_legacy`, "exclude from legend"
in the controller, and is exploited by `pivot_points` and `fib_retracement` to suppress
legend entries. A magic substring spanning three layers.

### 8. `finplot>=1.9` is unpinned
Despite `viewport.py` monkeypatching finplot/pyqtgraph private internals (`_xminmax`,
`_update_significants`, `update_y_zoom`, `v_zoom_scale`, …). A finplot point release
could silently break panning/zoom. Recommend pinning a tested version.

### 9. Hidden side-channel: `_daily_bars` injected into indicator params
`runtime.render_all()` does `ind_state.params["_daily_bars"] = daily_bars`, then on
symbol switch `controller._on_fetch_done` does `copy.deepcopy(s.params)` —
deep-copying the full daily-bar list (×N indicators) into per-symbol saved state. Both
a perf cost and a leaky abstraction (transient compute input riding inside persisted
params).

---

## 🟡 Low — cleanups & doc drift

- **10.** `PlotManager.scrub_orphan_markers()` hardcodes AVWAP's `"⚓"/"⚓️"` glyph —
  the generic chart layer knows about one specific indicator.
- **11.** Legacy migration cruft in `cache.py` (`_migrate` ALTERs on `avwap_anchors` +
  `_migrate_avwap_anchors`). `schema.sql` no longer defines that table, so this only
  serves users upgrading from a very old DB, and runs every launch. When can it go?
- **12.** `ChartWidget.wire_legend()` builds a placeholder `ChartLegend` in
  `_build_layout`, then immediately constructs a second one and deletes the first —
  build-twice-throw-one-away.
- **13. Doc drift:** `aggregator.py` module docstring says MIN39 is synthesized from
  MIN1, but `_SYNTHESIS_BASES` prefers MIN5 (the detailed comment lower down is correct
  — the top one is stale). `provider/base.py` says the aggregator "catches
  `UnsupportedTimeframeError`," but it actually pre-checks `native_timeframes()`.
  `AGENTS.md`'s API export list is out of sync with `api.py`'s `__all__`.

---

## ❓ Open questions
1. **Drawing-tool persistence:** `VerticalLineSessionStore` and
   `FibRetracementSessionStore` are **in-memory only** — they receive a
   persistence-capable `IndicatorStoreContext` but never use it, so lines/fibs vanish on
   restart (AVWAP persists via SQLite). Intended session-scope, or an unfinished
   feature?
2. **The two refactors (#1, #2):** finish and delete the old surface, or formally keep
   it as compatibility? That single decision drives a lot of the cleanup.
3. **Scope:** which of the safe mechanical wins (dead code #4, `SeriesFill` #5, mypy
   fixes #3, doc drift #13) to execute first while the bigger architectural calls
   (#1, #2) are discussed?
