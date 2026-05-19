# Compute Indicator Skill

Use this when creating or refactoring a compute-style SimpleChart indicator: an
ordinary chart overlay or panel indicator that primarily converts OHLCV data into
one or more aligned numpy arrays.

Do not use this as the main guide for drawing tools or indicators that need chart
context actions, drag handles, persistent drawing state, per-series
configuration/removal, or custom render primitives. Use
`docs/skills/interactive-indicator.md` for those.

Read this entire file before writing any code.

---

## Step 1 — Orient yourself

Read these files before classifying the work:

- `simplechart/api.py` — the public API surface; every import in a new indicator
  comes from here
- `indicators/_base.py` — the Indicator ABC, compute() and render() contracts,
  render primitives, interaction hooks, and default legacy render behavior

After the indicator is classified in Step 2, read the reference files that match
the capability you are about to build:

- `indicators/sma.py` — canonical example of a simple vectorized chart
  indicator with day-based period conversion and intraday warmup fill
- `indicators/ema/__init__.py` — same pattern as SMA but uses a recurrence-relation
  kernel (EMA cannot be vectorized); the clearest model for indicators that
  require a loop where each value depends on the previous one
- `indicators/avwap/__init__.py` — canonical example of a multi-output indicator
  with anchor-based series keys, custom render output, interaction hooks, and a
  compiled kernel. Read it here for stable key/render examples; use
  `interactive-indicator.md` for the full interactive process.
- `indicators/rsi/__init__.py` — reference example of panel routing: it
  overrides `render_target()` and draws in a dedicated panel below the chart
- `indicators/rsi/_kernel.py` — another recurrence-relation kernel example,
  using Wilder smoothing for RSI

These files are reference implementations for specific capabilities. Model the
parts that match the indicator you are building: SMA for vectorized chart
overlays, EMA/RSI for recurrence kernels, RSI for panel routing, and AVWAP for
stable keys/custom render output. Do not proceed until you have read the
required files for the relevant capability.

---

## Step 2 — Clarify requirements

Before writing any code, confirm these with the user:

1. **What does the indicator compute?** Get a clear description of the math/logic.
2. **Chart indicator or panel indicator?** Does it plot on the price chart (sharing
   the price y-axis), or does it need its own panel below with an independent y-axis?
   - Chart indicators: SMA, EMA, AVWAP — plotted at price scale
   - Panel indicators: RSI, MACD, RVOL — values on a different scale (0–100, etc.)
   - If panel: what short lowercase string should `render_target()` return?
     (e.g. `"rsi"`, `"macd"`) — this names the panel.
3. **Parameters?** What should the user be able to configure (period, color, etc.)?
4. **Default indicator?** Should it appear automatically for every symbol, or only when added?
5. **Plugin location?** Project plugins live in `indicators/`. User plugins can
   be dropped into `~/.simplechart/plugins/`.
   - Project plugins may be `.py` files or packages with `__init__.py`.
   - User plugins are currently loaded as `.py` files only.
6. **Does it need interactivity or persistence?** If it needs context actions,
   drag handles, persistent drawing state, per-series config/removal, or custom
   render primitives, switch to `docs/skills/interactive-indicator.md`.

---

## Step 3 — Research the indicator

Before producing the implementation spec, research the indicator's accepted
definition and calculation method. Use primary or authoritative technical
references when available, and cite the sources used.

Distill the research into a concise user-facing summary that establishes shared
understanding before implementation. Include:

- What the indicator measures or shows
- The exact equations or algorithm
- Required input data
- Timeframe/session assumptions
- Known variants and which variant(s) are in scope
- External libraries checked, including name, URL, license, and whether they
  were used as formula references, validation references, adapted code, copied
  code, or proposed dependencies
- Ambiguities or choices the user must resolve

Prefer external libraries for formulas and validation. Do not add a dependency
or copy/adapt implementation code without explicit user approval.

Wait for user approval of the research summary before finalizing the spec.

---

## Step 4 — Produce the indicator spec

Produce this spec and wait for user approval before reading conditional
reference files or editing code:

```text
Indicator type:
Plot target:
Inputs:
Outputs / series keys:
Parameters:
Formula / algorithm:
Timeframe / session assumptions:
Kernel decision:
Plugin location:
Persistence:
Default indicator:
Test plan:
```

---

## Step 5 — Decide: compiled kernel or not?

A compiled kernel is only justified when the computation involves a loop over
thousands of bars that cannot be expressed as a simple numpy vectorized operation.

Use this decision table:

| Computation shape | Kernel? | Rule |
|-------------------|---------|------|
| Simple array math such as ratios, differences, sums, comparisons, masks, or constant/reference arrays | No | Keep it in `compute()` with numpy. |
| Numpy already provides the operation clearly in one or a few vectorized calls | No | Prefer the numpy operation over a custom loop. |
| Rolling/windowed calculation that can use an existing project helper or clear vectorized numpy implementation | No | Do not add a kernel unless profiling shows this indicator is hot. |
| Recurrence relation where `result[i]` depends on `result[i-1]` | Yes | Use `_kernel.py` for EMA-style smoothing, Wilder smoothing, and similar loops. |
| Long per-bar loop that cannot be expressed clearly with numpy | Yes | Use `_kernel.py` and keep all I/O and object work outside it. |
| Interaction, persistence, key construction, labels, or small render-array assembly | No | Kernels are only for numeric hot paths. |

State your decision and reasoning to the user before writing any code.

---

## Step 6 — Phase gates

Proceed through these gates in order. Stop after each gate, explain what changed
or what decision was made, and wait for explicit user approval before continuing.

1. Requirements clarified
2. Domain research completed and summarized
3. Requirements spec approved
4. Conditional reference files read
5. Kernel decision approved
6. File plan approved
7. Kernel implemented, if needed
8. Indicator implemented
9. Default registration added, if requested
10. Tests and verification completed

---

## Step 7 — Implement

Work in this order, stopping after each piece to explain what you wrote and why.
Wait for the user to approve before moving to the next step.

### 7a. The kernel (if needed)

For single-file indicators: `indicators/your_indicator.py` (no kernel file needed —
put all logic in `compute()`).

For directory-form indicators (kernel needed):
```
indicators/
  your_indicator/
    __init__.py     # Indicator subclass + register_indicator()
    _kernel.py      # compiled kernel
```

File: `indicators/your_indicator/_kernel.py`

Rules — required for mypyc compilation:
- Every parameter and return value must have an explicit type annotation
- Accept `np.ndarray` and plain Python scalars (`int`, `float`) only
- Return `np.ndarray` (or `list[np.ndarray]` for multiple outputs)
- Use `float()` and `int()` to convert numpy scalars in loop bodies
- Pre-compute loop-invariant values outside the loop
- No I/O of any kind (no files, no SQLite, no network, no print)
- No `Any`, no `Union` in function signatures
- No `ABCMeta`, no `getattr`/`setattr`, no `**kwargs`
- No default mutable arguments

After writing the kernel, add the module path to `pyproject.toml`:
```toml
[tool.simplechart.compile]
targets = [
    ...
    "indicators.your_indicator._kernel",
]
```

Reference kernels:
- `indicators/ema/_kernel.py` — single-output kernel (`np.ndarray` return)
- `indicators/avwap/_kernel.py` — multi-output kernel (`list[np.ndarray]` return);
  use this as the model when the kernel produces more than one array (e.g.
  upper band, middle band, lower band for Bollinger Bands)

### 7b. The Indicator subclass

File: `indicators/your_indicator.py` (single-file) or
      `indicators/your_indicator/__init__.py` (directory form with kernel)

Imports come from `simplechart.api`:
```python
from simplechart.api import (
    Indicator,
    ChoiceParam,
    LINE_STYLE_OPTIONS,
    register_indicator,
    OHLCVSeries,
)
```

Required methods:
- `name(self) -> str` — unique machine-readable key used as registry key and
  series key prefix (e.g. `"sma"`, `"avwap"`)
- `label(self) -> str` — human-readable name for the legend and config dialog
- `default_params(self) -> dict[str, Any]` — parameter defaults; the config
  dialog infers input widget types automatically:
  - `int` → spin box
  - `float` → decimal spin box
  - `str` starting with `"#"` → color picker
  - `ChoiceParam` → dropdown
  - Parameter names should be `snake_case`

  Standard visual params — include these for every indicator that draws lines:
  ```python
  "color":       "#RRGGBB",
  "line_width":  1.0,
  "line_style":  ChoiceParam("solid", LINE_STYLE_OPTIONS),
  ```

- `compute(self, series, params) -> dict[str, np.ndarray]` — returns named
  arrays aligned to `series.bars` (same length); use `np.nan` for invalid bars

Key rules for `compute()`:
- Series keys must be stable and unique across calls — if a key changes between
  calls, the chart creates a new plot line instead of updating the existing one
- Use the pattern `f"{name}_{param}"` for keys (e.g. `"sma_50"`)
- For day-based periods, use `bars_for_n_days(days, series.timeframe)` from
  `simplechart.api` — this keeps price values consistent across all timeframes
- Delegate heavy numeric work to the kernel; `compute()` is called on every
  symbol load and timeframe switch

**For panel indicators only** — override `render_target()`:
```python
def render_target(self) -> str:
    return "your_panel_name"   # e.g. "rsi", "macd"
```

Chart indicators do not override this method.

End the file with: `register_indicator(YourIndicator)`

`simplechart.plugins` loads every `.py` file and sub-package in the project
plugin package whose name does not start with `_`, plus user plugin `.py` files
from `~/.simplechart/plugins/`. No app/controller import wiring is needed — the
`register_indicator()` call at the bottom of the file fires on import.

### 7c. Add to INITIAL_INDICATORS (optional)

File: `app/controller.py`

Only if the indicator should appear on every chart automatically:
```python
("your_indicator", {"days": 14, "color": "#DA70D6", "line_width": 1.0}),
```
Params not listed here fall back to `default_params()`. `line_style` is a
`ChoiceParam` and is not set here — it comes from `default_params()` as-is.

---

## Step 8 — Verify

After all steps are complete:

1. Launch the app: `simplechart`
2. Load any symbol
3. Confirm the indicator appears (if default) or can be added via the UI
4. For panel indicators: confirm it appears in a dedicated panel below volume
5. Switch timeframes and confirm the values remain correct
6. If a kernel was added, optionally compile it: `python scripts/build_compiled.py`
7. Run these checks:
   `python -m compileall app chart indicators simplechart tests`
   `python -m pytest`
8. If you need a focused mypy check, ask the user before choosing the target,
   then run:
   `python -m mypy --follow-imports=skip path/to/touched_file.py`

---

## Engineering rules (apply throughout)

- All new and modified functions must be fully typed: parameters and return values
- Do not add error handling for scenarios that cannot happen
- Do not add docstrings, type hints, or comments to existing code you did not change
- Build only what is needed — no speculative parameters or future-proofing
- Prefer simple, readable code over clever code
