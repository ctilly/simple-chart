# Performance Proposal

The best next performance project is to remove repeated Python-side bar extraction from indicator recomputation, especially for AVWAP and any future multi-line indicators. I would not start by compiling more chart code. I would start by making indicator inputs reusable and then, only if needed, move a small helper into mypyc.

## Phase 1

Add a cached array bundle for each loaded series.

Introduce a small core type, likely in `data/models.py` or a new core file such as `data/series_arrays.py`, that holds:

- `highs: np.ndarray`
- `lows: np.ndarray`
- `closes: np.ndarray`
- `volumes: np.ndarray`
- `bar_ts_ms: list[int]`

Then add one builder function that converts `OHLCVSeries.bars` once per load. The controller would build it when a new series arrives and pass it into indicator computation, or store it on the active series state.

This gives the biggest immediate win because it removes repeated list comprehensions in:

- `plugins/builtin/sma.py`
- `plugins/builtin/ema.py`
- `plugins/builtin/avwap.py`

## Phase 2

Refine the indicator API without making it complicated.

Keep the current `Indicator.compute(series, params)` contract working, but add one of these:

1. A new optional `compute_fast(series, arrays, params)` path.
2. Or a small `IndicatorContext` object that contains both `series` and prebuilt arrays.

I prefer the context object because it scales better as more indicators appear, and it avoids widening every method signature ad hoc.

## Phase 3

Compile only the new hot helper if profiling justifies it.

If array extraction is still expensive after the refactor, then add a narrow mypyc target such as `indicators._fast.series_extract` or `data._fast.resample`. Keep it primitive-only:

- input lists or primitive arrays
- output numpy arrays or primitive lists
- no Qt
- no dataclasses
- no SQLite
- no dict-heavy orchestration

## Phase 4

Only consider aggregator compilation after measurement.

`data/aggregator.py` is a possible future target, but not in its current shape. If synthesized timeframe generation proves slow, extract a pure helper that works on primitive arrays and precomputed grouping indexes, then compile that helper. Do not try to compile the current object-heavy aggregator module directly.

## Expected Impact

The likely real gain is lower recompute latency during:

- symbol loads
- timeframe switches
- adding/removing AVWAP anchors
- recomputing multiple indicators on the same bar set

The gain will be more noticeable as you add more indicators or more anchors.

## Recommended Order

1. Add cached array bundle for `OHLCVSeries`
2. Route SMA, EMA, and AVWAP to use it
3. Measure
4. Only then decide whether a new mypyc helper is worth adding

## Follow-up

Next step can be a concrete implementation plan with exact file-by-file changes and a low-risk migration path.
