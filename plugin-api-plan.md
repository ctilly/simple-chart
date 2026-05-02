# Plugin API Implementation Plan

## Goal

Deliver a professional, low-coupling indicator plugin system with two properties:

1. **Stable public API** — plugin authors import from `simplechart.api` only.
   Internal reorganization never breaks external plugins.
2. **Auto-discovery** — drop a `.py` file (or directory) into `indicators/` and it
   loads automatically. No manual wiring inside the source tree.

Additionally: fix RSI, which is registered but invisible because its values
(0–100) are drawn on the price chart alongside $100+ stock prices. This requires
a dedicated indicator panel.

The `Indicator` ABC, registry, `compute()` contract, and mypyc compilation path
are all correct and do not change. This plan is about the import surface, the
loading infrastructure, and the indicator panel system.

---

## Terminology

Two categories of indicators, distinguished by where they draw:

- **Chart indicators** — plot directly on the price chart (time × price axes).
  SMA, EMA, AVWAP. Their y-axis is price; they share the same axis as the
  candlesticks.
- **Panel indicators** — draw in a dedicated panel below the chart. RSI, MACD,
  RVOL. They share only the x (time) axis with the price chart; their y-axis is
  independent and scaled to the indicator's own value range.

"Chart" and "panel" are the canonical terms throughout the codebase and docs.
Do not use "overlay" or "oscillator" — those terms are retired.

---

## Design decisions

### Why `simplechart/api.py` and not `indicators/api.py`

`simplechart.api` is the top-level public identity of the project. It makes no
claim about internal package layout. An author writing
`from simplechart.api import Indicator` does not need to know — or care — that
`Indicator` lives in `indicators/_base.py`. If `indicators/` is ever reorganized,
`simplechart/api.py` is the only file that changes. External plugins are unaffected.

### Public API surface

Only what a plugin author needs to write and register an indicator:

| Name                        | Source                 | Purpose                                    |
|-----------------------------|------------------------|--------------------------------------------|
| `Indicator`                 | `indicators._base`     | ABC to subclass                            |
| `ChoiceParam`               | `indicators._base`     | Dropdown param type                        |
| `LINE_STYLE_OPTIONS`        | `indicators._base`     | Standard line style choices                |
| `RENDER_CHART`              | `indicators._base`     | `render_target()` constant for chart indicators |
| `SeriesFill`                | `indicators._base`     | Declares a shaded fill between two series  |
| `register`                  | `indicators._registry` | Register the indicator class               |
| `OHLCVSeries`               | `data.models`          | Series type passed to `compute()`          |
| `Bar`                       | `data.models`          | Individual bar type                        |
| `AnchorRecord`              | `data.models`          | Anchor type for AVWAP-style indicators     |
| `bars_for_n_days`           | `data.calendar`        | Day-based period → bar count conversion    |
| `timestamp_ms_to_bar_index` | `data.calendar`        | UTC ms timestamp → bar index               |

Internal-only functions (`get`, `all_indicators`) stay on `indicators._registry`.
App code (controller, etc.) may import from internals freely — the public API
boundary is for plugin authors, not internal code.

### `render_target()` — routing chart vs. panel indicators

The `Indicator` ABC has one concrete (non-abstract) method:

```python
def render_target(self) -> str:
    return RENDER_CHART   # "chart"
```

- **Chart indicators** do not override this. They draw on the price chart.
- **Panel indicators** override it and return a short, unique lowercase string
  that names their panel — e.g. `"rsi"`, `"macd"`, `"rvol"`.

The return value is both the routing key and the panel identity:
- Two RSI instances (14-day and 21-day) both return `"rsi"` and share one panel.
- RSI and MACD return different strings and get separate panels.
- Panel indicators from different plugins that intentionally share a panel can
  do so by returning the same string.

There is no `RENDER_PANEL` constant — panel indicators just return their own
unique string. Only `RENDER_CHART` is exported, since chart is the default.

### Dynamic indicator panels

Up to 3 indicator panels may be open simultaneously alongside price and volume.
Panels are **not visible until an indicator that requires one is added**. When
the last indicator using a panel is removed, the panel disappears.

**Implementation approach** — pre-allocated slots with variable height:

`create_plot_widget(master, rows=5)` creates 5 finplot axes at startup:
- Row 0: price panel (stretch 4)
- Row 1: volume panel (stretch 1)
- Rows 2–4: indicator panel slots (stretch 0 — zero height, invisible)

When a panel indicator is added, the first unoccupied slot is assigned to that
panel's `render_target()` name and its stretch factor is set to 2. When the last
indicator using a slot is removed, the slot's data is cleared and its stretch is
reset to 0.

**Why pre-allocated rather than dynamic axis creation:**
`viewport.py`'s `_linked_x_viewboxes()` discovers panels via `viewbox.win.axs`,
which only sees axes in the same `_FinplotMaster`. Panels created in separate
Qt widgets would not be found by the x-range persistence logic, causing them to
fall out of sync after redraws. Pre-allocating in the same master avoids this
entirely.

**Panel close / removal:** via the legend's existing X button (Option C). When
the user removes the last indicator in a panel via the legend, the panel
disappears automatically. No separate close button on the panel itself.

### Panel limit

Maximum 3 indicator panels. If all 3 slots are occupied and the user adds a
fourth panel-type indicator, a `RuntimeError` is raised (caught by the
controller, shown as a warning dialog). The limit is enforced in `ChartWidget`.

### Auto-discovery mechanics

A directory scanner imports every `.py` file and sub-package whose name does not
start with `_`, sorted alphabetically. The `register()` call at the bottom of
each indicator fires automatically on import — exactly as before. No other
mechanism is needed.

The `indicators/` directory is the single location for all indicators. The
scanner (`indicators/_loader.py`) is called once at app startup from
`app/controller.py`. Errors propagate — a broken indicator is a bug.

Infrastructure files (`_base.py`, `_registry.py`, `_loader.py`, `__init__.py`)
all start with `_` and are skipped by the scanner automatically.

---

## Implementation order

Each phase is reviewed and approved before the next begins.

---

## ✅ Phase 1 — Create `simplechart/api.py`

**New files:**

```
simplechart/__init__.py    # minimal package marker; no imports
simplechart/api.py         # public re-export surface
```

### `simplechart/__init__.py`
One-line module docstring. No imports. No re-exports.

### `simplechart/api.py`
Re-exports only — no implementation logic. Full module docstring covering:
- What this API is and who it is for
- A complete, working minimal indicator example using only
  `from simplechart.api import ...` imports
- Table of all exports with one-line descriptions

Each re-export gets a brief inline comment stating its role.

### `indicators/_base.py`
Added to the `Indicator` ABC:
- `RENDER_CHART: str = "chart"` module-level constant
- `render_target() -> str` concrete method (returns `RENDER_CHART`)
- `series_fills() -> list[SeriesFill]` concrete method (returns `[]`)
- `SeriesFill` dataclass
- `ChoiceParam` dataclass

### `pyproject.toml`
Added `"simplechart*"` to the `[tool.setuptools.packages.find]` include list.

---

## ✅ Phase 2 — Directory restructure and auto-discovery

Replaced the `plugins/` + `indicators/_fast/` split with a single `indicators/`
directory containing both the infrastructure and all indicator implementations.

### `indicators/` structure (final)

```
indicators/
  __init__.py      # minimal package marker
  _base.py         # Indicator ABC, RENDER_CHART, SeriesFill, LINE_STYLE_OPTIONS
  _registry.py     # register(), get(), all_indicators()
  _loader.py       # discovery scanner
  sma.py           # SMA — single file (vectorized numpy, no kernel needed)
  ema/             # EMA — directory form (sequential loop benefits from mypyc)
    __init__.py    # EMAIndicator class + register()
    _kernel.py     # ema() kernel
  avwap/           # AVWAP — directory form (double loop benefits from mypyc)
    __init__.py    # AVWAPIndicator class + register()
    _kernel.py     # avwap_multi() kernel (dead avwap() single-anchor removed)
  rsi.py           # RSI — moved from plugins/example_plugin.py, cleaned up
```

### `indicators/_loader.py`

Single public function `load_indicators(indicators_dir: Path) -> None`.

Uses `importlib.import_module` directly — much simpler than
`spec_from_file_location` since all indicators live inside the `indicators`
package. Python's import machinery (via `sys.modules`) prevents double-loading.

- `.py` files not starting with `_` → `importlib.import_module("indicators.{stem}")`
- Directories not starting with `_` with an `__init__.py` →
  `importlib.import_module("indicators.{name}")`
- All errors propagate — a broken indicator is a bug.

### Built-in indicator imports

Built-in indicators (`sma.py`, `ema/`, `avwap/`, `rsi.py`) import directly from
the internal modules (`indicators._base`, `indicators._registry`) since they are
part of the same package. External plugin authors use `simplechart.api` instead.

### `app/controller.py`
- Removed: `import plugins  # noqa: F401`
- Updated: `from indicators.base import ...` → `from indicators._base import ...`
- Updated: `from indicators.registry import ...` → `from indicators._registry import ...`
- Added at module level:
```python
from indicators._loader import load_indicators as _load_indicators
from pathlib import Path

_load_indicators(Path(__file__).parent.parent / "indicators")
```

### `app/indicator_config.py`
- `from indicators.base import ChoiceParam` → `from indicators._base import ChoiceParam`

### `simplechart/api.py`
- `from indicators.base import ...` → `from indicators._base import ...`
- `from indicators.registry import register` → `from indicators._registry import register`

### `pyproject.toml`
- Removed `"plugins*"` from `[tool.setuptools.packages.find]` include list.
- Updated `[tool.simplechart.compile]` targets:
  - Removed `"indicators._fast.ma"` and `"indicators._fast.avwap"`
  - Added `"indicators.ema._kernel"` and `"indicators.avwap._kernel"`

### Deleted
- `indicators/base.py` — renamed to `_base.py`
- `indicators/registry.py` — renamed to `_registry.py`
- `indicators/_fast/` directory and all contents
- `plugins/` directory and all contents

---

## Phase 3 — Add indicator panel support

This phase fixes the chart architecture required before RSI can work. It touches
four files. Read all four before writing any code.

### `indicators/_base.py`
No changes needed — `render_target()` and `RENDER_CHART` were already added in
Phase 1. Indicator subclasses override `render_target()` to opt into a panel.

### `chart/panel.py`
- Add `INDICATOR = auto()` to `PanelType`.
- Add `is_indicator` property.
- Add a new `IndicatorPanelSlot` dataclass:

```python
@dataclass
class IndicatorPanelSlot:
    panel: Panel            # wraps the pre-allocated finplot axis
    name: str | None        # render_target string, or None if unoccupied
```

Three slots (indices 0–2) are created at startup and managed by `ChartWidget`.

### `chart/window.py`
- Change `create_plot_widget(master, rows=2, ...)` to `rows=5`.
- Axes 0–1: price and volume panels, same as today.
- Axes 2–4: indicator panel slots, initial stretch = 0.
- Store slots as `self._indicator_slots: list[IndicatorPanelSlot]`.
- Add `ensure_indicator_panel(name: str) -> Panel`:
  - If a slot is already assigned `name`, return its panel.
  - Otherwise assign the first free slot, set its stretch to 2, return the panel.
  - If all 3 slots are occupied by other names, raise `RuntimeError`.
- Add `release_indicator_panel(name: str) -> None`:
  - Find the slot assigned `name`.
  - Clear the axis (call `ax.reset()`).
  - Set its stretch back to 0.
  - Mark the slot as unoccupied.
- Update `reset_viewport` to include all active (non-zero-stretch) indicator
  panel axes.
- Update `clear_all` to call `release_indicator_panel` for all occupied slots.

### `chart/viewport.py`
Add a new function:

```python
def install_indicator_panel_behavior(panel_ax: object, price_ax: object) -> None:
```

This links the indicator panel's x-axis to the price panel and configures
appropriate interaction:
- `panel_ax.vb.setXLink(price_ax.vb)` — synchronize x-range.
- `panel_ax.vb.setMouseEnabled(x=True, y=False)` — x-pan only; y is auto-scaled.
- `_patch_update_y_zoom(panel_ax.vb)` — apply the same y-zoom patch used for
  price and volume so the panel auto-fits its visible data.

Called by `ChartWidget.ensure_indicator_panel()` the first time a slot is
assigned.

### `chart/plot_manager.py`
- Constructor gains an `indicator_slots` parameter:
  `list[IndicatorPanelSlot]` (the same list owned by `ChartWidget`).
- `update_indicator()` gains a `render_target: str` parameter.
  - If `render_target == RENDER_CHART`: draw on `_price_panel.ax` (existing behavior).
  - Otherwise: find the slot whose `name == render_target`, draw on that slot's axis.
- `clear_all()` already calls `ax.reset()` on price and volume panels; no
  change needed for indicator slots (handled by `ChartWidget.clear_all()`).

### `app/controller.py`
- In `_compute_and_draw()`:
  - Call `indicator.render_target()` on the indicator instance.
  - If not `RENDER_CHART`, call `self._chart.ensure_indicator_panel(target)`
    before the draw loop.
  - Pass `render_target` to `PlotManager.update_indicator()`.
- In `_on_indicator_remove()`:
  - After removing all series keys for an indicator, check whether any remaining
    active indicators share the same `render_target`.
  - If none remain, call `self._chart.release_indicator_panel(target)`.

**Check:** Launch the app. Price and volume panels appear as before. No indicator
panels are visible. SMA/EMA/AVWAP are unaffected.

---

## Phase 4 — Fix RSI

### Root cause of RSI being invisible
RSI is registered and computes correct values. But `render_target()` is not yet
overridden, so it draws on the price chart where 0–100 RSI values are invisible
against $100+ stock prices. The panel from Phase 3 is the fix.

### Changes to `indicators/rsi.py`
1. Override `render_target()` to return `"rsi"`.
2. Add reference lines for overbought (70) and oversold (30) levels using
   finplot's `add_line()` or `plot()` with a horizontal constant array —
   check `chart/plot_manager.py` first; if an `add_panel_reference_line()`
   helper does not exist, add it to `PlotManager` as part of this phase.

**Check:** Add RSI via the Add Indicator menu. It appears in a dedicated RSI
panel below volume. Overbought (70) and oversold (30) reference lines are
visible. SMA/EMA/AVWAP are unaffected. Removing RSI from the legend removes the
panel.

---

## Phase 5 — Update documentation and skill files

### `indicators/_base.py`
Docstring is already updated (done in Phase 2). Verify it accurately reflects the
current architecture.

### `.claude/skills/new-indicator.md`
- Add `simplechart/api.py` to Step 1 (read this file first).
- Update all import examples to use `simplechart.api`.
- Update Step 2 (clarify requirements) to explicitly ask: chart indicator or
  panel indicator? If panel, what `render_target()` string?
- Add a step showing the `render_target()` override for panel indicators.
- Replace "overlay" / "oscillator" with "chart" / "panel" throughout.

### `AGENTS.md`
Add a brief "Indicator API" section: `simplechart.api` is the stable public
import path; internal package paths are not part of the public contract.

### `README.md`
Update the "Building Custom Indicators" section to show `simplechart.api` as
the starting import and explain the chart vs. panel distinction.

---

## File summary

### Created (Phases 1–2, complete)

| File                          | Description                                          |
|-------------------------------|------------------------------------------------------|
| `simplechart/__init__.py`     | Package marker                                       |
| `simplechart/api.py`          | Public API re-export module                          |
| `indicators/_loader.py`       | Directory-scanning indicator loader                  |
| `indicators/_base.py`         | Indicator ABC and constants (renamed from base.py)   |
| `indicators/_registry.py`     | Registry (renamed from registry.py)                  |
| `indicators/sma.py`           | SMA indicator (moved from plugins/builtin/)          |
| `indicators/ema/__init__.py`  | EMA indicator class (moved from plugins/builtin/)    |
| `indicators/ema/_kernel.py`   | EMA compiled kernel (from indicators/_fast/ma.py)    |
| `indicators/avwap/__init__.py`| AVWAP indicator class (moved from plugins/builtin/)  |
| `indicators/avwap/_kernel.py` | AVWAP compiled kernel (from indicators/_fast/avwap.py) |
| `indicators/rsi.py`           | RSI indicator (moved from plugins/example_plugin.py) |

### Modified (Phases 1–2, complete)

| File                      | Change                                                              |
|---------------------------|---------------------------------------------------------------------|
| `pyproject.toml`          | Added `simplechart*`; removed `plugins*`; updated compile targets   |
| `app/controller.py`       | Replaced `import plugins` with `load_indicators()` call; updated imports |
| `app/indicator_config.py` | Updated import to `indicators._base`                                |
| `simplechart/api.py`      | Updated imports to `_base` and `_registry`                          |

### To be modified (Phases 3–5)

| File                              | Change                                                        |
|-----------------------------------|---------------------------------------------------------------|
| `chart/panel.py`                  | Add `INDICATOR` panel type; add `IndicatorPanelSlot`          |
| `chart/plot_manager.py`           | Route draws via `render_target`; add reference line helper    |
| `chart/window.py`                 | 5-row master; slot management; `ensure/release_indicator_panel` |
| `chart/viewport.py`               | Add `install_indicator_panel_behavior()`                      |
| `app/controller.py`               | Pass `render_target` in `_compute_and_draw`; release panels on remove |
| `indicators/rsi.py`               | Override `render_target()`; add overbought/oversold reference lines |
| `.claude/skills/new-indicator.md` | Update imports; chart vs. panel guidance                      |
| `AGENTS.md`                       | Add indicator API section                                     |
| `README.md`                       | Update indicator building section                             |

### Deleted (Phase 2, complete)

| File / Directory                  | Reason                                               |
|-----------------------------------|------------------------------------------------------|
| `indicators/base.py`              | Renamed to `_base.py`                                |
| `indicators/registry.py`          | Renamed to `_registry.py`                            |
| `indicators/_fast/`               | Kernels moved into indicator directories             |
| `plugins/`                        | Entire directory — all contents moved to `indicators/` |

---

## What does not change

- `Indicator` ABC methods: `name()`, `label()`, `default_params()`, `compute()`
- `compute()` contract: return type, array alignment, `np.nan` convention
- `ChoiceParam` and `LINE_STYLE_OPTIONS`
- `DEFAULT_INDICATORS` in `app/controller.py`
- All data layer code

---

## Future work (out of scope for this plan)

### Bollinger Bands
A chart indicator that plots three lines (upper band, middle SMA, lower band)
with a shaded fill between the upper and lower bands.

Requires rendering support for `series_fills()` in `PlotManager` — specifically
`fplt.fill_between(series_a, series_b, ax=..., color=...)`. The `SeriesFill`
dataclass and `series_fills()` ABC method are already defined so a plugin author
can declare fills today; the chart layer just won't draw them until this work is
done.

Implementation checklist (when scheduled):
- [ ] `PlotManager.update_indicator()` reads `indicator.series_fills()` and calls
      `fplt.fill_between()` for each declared fill after drawing the lines.
- [ ] `PlotManager.remove_indicator()` also removes any fill objects.
- [ ] `PlotManager.clear_all()` handles fill cleanup.
- [ ] `indicators/bollinger.py` — new built-in using `series_fills()`.

### Volume Profile
A panel indicator that draws horizontal bars showing volume traded at each
price level over a session or time range. Visually distinct from line-based
indicators — requires a bar/histogram rendering path rather than `fplt.plot()`.

Implementation requires understanding finplot's `fplt.bar()` or a custom
pyqtgraph bar item, and a `compute()` shape that returns price-level buckets
rather than time-aligned arrays (different from the standard contract). Design
work needed before implementation begins.
