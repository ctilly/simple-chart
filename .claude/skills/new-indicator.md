# New Indicator Skill

You are helping build a new technical indicator for SimpleChart, a desktop stock
charting application written in Python. Read this entire file before writing any code.

---

## Step 1 — Orient yourself

Read these files before doing anything else:

- `indicators/base.py` — the Indicator ABC, the compute() contract, and the
  complete step-by-step guide for new indicators (it is thorough; read all of it)
- `plugins/builtin/sma.py` — canonical example of a simple overlay indicator
  with a compiled kernel, day-based period conversion, and warmup fill
- `plugins/builtin/ema.py` — same pattern as SMA but uses a recurrence-relation
  kernel (EMA cannot be vectorized); the clearest model for indicators that
  require a loop where each value depends on the previous one
- `plugins/builtin/avwap.py` — canonical example of a multi-output indicator
  with anchor-based series keys and a compiled kernel

These files are the reference implementations. Model new indicators on them.
Do not proceed until you have read all four.

---

## Step 2 — Clarify requirements

Before writing any code, confirm these with the user:

1. **What does the indicator compute?** Get a clear description of the math/logic.
2. **Overlay or separate panel?** Does it plot on the price chart, or does it need
   its own panel below? Check `chart/panel.py` if a separate panel is needed.
3. **Parameters?** What should the user be able to configure (period, color, etc.)?
4. **Default on every chart?** Should it appear automatically, or only when added?
5. **Built-in or third-party?** Built-ins go in `plugins/builtin/`. Third-party
   plugins can live anywhere on the Python path.

---

## Step 3 — Decide: compiled kernel or not?

A compiled kernel in `indicators/_fast/` is only justified when the computation
involves a loop over thousands of bars that cannot be expressed as a simple
numpy vectorized operation.

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
File: `indicators/_fast/your_kernel.py`

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

After writing the kernel, also add the module path to `pyproject.toml`:
```toml
[tool.simplechart.compile]
targets = [
    ...
    "indicators._fast.your_kernel",
]
```

Reference kernels:
- `indicators/_fast/ma.py` — single-output kernels (`np.ndarray` return)
- `indicators/_fast/avwap.py` — multi-output kernel (`list[np.ndarray]` return);
  use this as the model when the kernel produces more than one array (e.g.
  upper band, middle band, lower band for Bollinger Bands)

### 4b. The Indicator subclass
File: `plugins/builtin/your_indicator.py` (built-in) or wherever the user specifies

Required methods:
- `name(self) -> str` — unique machine-readable key used as registry key and
  series key prefix (e.g. `"sma"`, `"avwap"`)
- `label(self) -> str` — human-readable name for the legend and config dialog
- `default_params(self) -> dict[str, Any]` — parameter defaults; the config
  dialog infers input widget types automatically:
  - `int` → spin box
  - `float` → decimal spin box
  - `str` starting with `"#"` → color picker
  - `ChoiceParam` → dropdown (import from `indicators.base`)
  - Parameter names should be `snake_case`

  Standard visual params — include these for every indicator that draws lines:
  ```python
  "color":       "#RRGGBB",
  "line_width":  1.0,
  "line_style":  ChoiceParam("solid", LINE_STYLE_OPTIONS),
  ```
  Import both `ChoiceParam` and `LINE_STYLE_OPTIONS` from `indicators.base`.
  See `plugins/builtin/sma.py` for the exact pattern.

  For indicators that also draw fills (e.g. Bollinger Bands), add:
  ```python
  "fill_color":   "#RRGGBB",
  "fill_alpha":   0.15,       # float 0.0–1.0
  ```
  Check `chart/plot_manager.py` to see how fill params are consumed before
  deciding on exact param names.
- `compute(self, series, params) -> dict[str, np.ndarray]` — returns named
  arrays aligned to `series.bars` (same length); use `np.nan` for invalid bars

Key rules for `compute()`:
- Series keys must be stable and unique across calls — if a key changes between
  calls, the chart creates a new plot line instead of updating the existing one
- Use the pattern `f"{name}_{param}"` for keys (e.g. `"sma_50"`)
- For day-based periods, use `bars_for_n_days(days, series.timeframe)` from
  `data.calendar` — this keeps price values consistent across all timeframes
- Delegate heavy numeric work to the kernel; `compute()` is called on every
  symbol load and timeframe switch

End the file with: `register(YourIndicator)`

### 4c. Register the import (built-ins only)
File: `plugins/builtin/__init__.py`

Add one line:
```python
from plugins.builtin import your_indicator  # noqa: F401
```

### 4d. Add to DEFAULT_INDICATORS (optional)
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
4. Switch timeframes and confirm the values remain correct
5. If a kernel was added, optionally compile it: `python scripts/build_compiled.py`

---

## Engineering rules (apply throughout)

- All new and modified functions must be fully typed: parameters and return values
- Do not add error handling for scenarios that cannot happen
- Do not add docstrings, type hints, or comments to existing code you did not change
- Build only what is needed — no speculative parameters or future-proofing
- Prefer simple, readable code over clever code
