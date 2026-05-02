# New Indicator Skill

You are helping build a new technical indicator for SimpleChart, a desktop stock
charting application written in Python. Read this entire file before writing any code.

---

## Step 1 — Orient yourself

Read these files before doing anything else:

- `simplechart/api.py` — the public API surface; every import in a new indicator
  comes from here
- `indicators/_base.py` — the Indicator ABC, the compute() contract, and the
  complete step-by-step guide for new indicators (it is thorough; read all of it)
- `indicators/sma.py` — canonical example of a simple chart indicator with a
  compiled kernel, day-based period conversion, and warmup fill
- `indicators/ema/__init__.py` — same pattern as SMA but uses a recurrence-relation
  kernel (EMA cannot be vectorized); the clearest model for indicators that
  require a loop where each value depends on the previous one
- `indicators/avwap/__init__.py` — canonical example of a multi-output indicator
  with anchor-based series keys and a compiled kernel
- `indicators/rsi.py` — canonical example of a panel indicator that overrides
  `render_target()` and draws in a dedicated panel below the chart

These files are the reference implementations. Model new indicators on them.
Do not proceed until you have read all six.

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
5. **Built-in or external?** Built-ins go in `indicators/`. External plugins can
   live anywhere on the Python path.

---

## Step 3 — Decide: compiled kernel or not?

A compiled kernel is only justified when the computation involves a loop over
thousands of bars that cannot be expressed as a simple numpy vectorized operation.

**No kernel needed** (numpy handles it directly in `compute()`):
- Simple array math: ratios, differences, sums
- Anything numpy's built-in operations can cover in a single pass

**Kernel needed** (tight loop, value depends on previous value):
- Recurrence relations: EMA-style smoothing, Wilder smoothing, running sums
  where `result[i]` depends on `result[i-1]`

State your decision and reasoning to the user before writing any code.

---

## Step 4 — Implement

Work in this order, stopping after each piece to explain what you wrote and why.
Wait for the user to approve before moving to the next step.

### 4a. The kernel (if needed)

For single-file indicators: `indicators/your_indicator.py` (no kernel file needed —
put all logic in `compute()`).

For directory-form indicators (kernel needed):
```
indicators/
  your_indicator/
    __init__.py     # Indicator subclass + register()
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

### 4b. The Indicator subclass

File: `indicators/your_indicator.py` (single-file) or
      `indicators/your_indicator/__init__.py` (directory form with kernel)

Imports come from `simplechart.api`:
```python
from simplechart.api import (
    Indicator,
    ChoiceParam,
    LINE_STYLE_OPTIONS,
    register,
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

End the file with: `register(YourIndicator)`

Auto-discovery (`indicators/_loader.py`) loads every `.py` file and sub-package
in `indicators/` whose name does not start with `_`. No manual import wiring
is needed — the `register()` call at the bottom of the file fires on import.

### 4c. Add to DEFAULT_INDICATORS (optional)

File: `app/controller.py`

Only if the indicator should appear on every chart automatically:
```python
("your_indicator", {"days": 14, "color": "#DA70D6", "line_width": 1.0}),
```
Params not listed here fall back to `default_params()`. `line_style` is a
`ChoiceParam` and is not set here — it comes from `default_params()` as-is.

---

## Step 5 — Verify

After all steps are complete:

1. Launch the app: `simplechart`
2. Load any symbol
3. Confirm the indicator appears (if default) or can be added via the UI
4. For panel indicators: confirm it appears in a dedicated panel below volume
5. Switch timeframes and confirm the values remain correct
6. If a kernel was added, optionally compile it: `python scripts/build_compiled.py`

---

## Engineering rules (apply throughout)

- All new and modified functions must be fully typed: parameters and return values
- Do not add error handling for scenarios that cannot happen
- Do not add docstrings, type hints, or comments to existing code you did not change
- Build only what is needed — no speculative parameters or future-proofing
- Prefer simple, readable code over clever code
