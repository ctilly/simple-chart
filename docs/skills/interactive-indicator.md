# Interactive Indicator Skill

Use this when creating or refactoring an interactive SimpleChart indicator:
drag handles, chart context actions, persistent drawing state, per-series
configuration/removal, or indicator-owned store behavior.

If the indicator only computes arrays from OHLCV data and is configured through
the normal indicator dialog, use `docs/skills/compute-indicator.md` instead.

If the work is a **toolbar drawing tool** the user places and manipulates on the
chart (vertical line, Fibonacci retracement) with `DrawingStore`-backed
timeframe/session persistence, use `docs/skills/drawing-tool.md` instead. This
skill is for interactive *indicators* (e.g. Anchored VWAP), which compute from
data, are added via context menu, and always persist.

## Read First

Read these files before classifying the work:

- `simplechart/api.py` — public plugin API. Indicator behavior imports from here.
- `simplechart/plugins.py` — plugin loading model.
- `app/extension_runtime.py` — generic event/render/config/remove routing.
- `app/extension_store.py` — generic store-handler boundary.

After the indicator is classified, read only the reference files needed for the
capabilities you will implement:

- `indicators/avwap/__init__.py` — reference interactive indicator behavior.
- `indicators/avwap/anchor_store.py` — reference persistent store handler.
- `chart/plot_manager.py` and `chart/interactions.py` — generic chart rendering/input.

## Research

Before producing the implementation spec, research the indicator's accepted
definition, interaction model, and calculation method. Use primary or
authoritative technical references when available, and cite the sources used.

Distill the research into a concise user-facing summary that establishes shared
understanding before implementation. Include:

- What the indicator, tool, or drawing shows
- The exact equations, algorithm, or interaction rules
- Required input data and chart coordinates
- Timeframe/session assumptions
- Known variants and which variant(s) are in scope
- Persistence assumptions, if any
- External libraries checked, including name, URL, license, and whether they
  were used as formula references, validation references, adapted code, copied
  code, or proposed dependencies
- Ambiguities or choices the user must resolve

Prefer external libraries for formulas and validation. Do not add a dependency
or copy/adapt implementation code without explicit user approval.

Wait for user approval of the research summary before finalizing the spec.

## Indicator Spec

Before editing code, produce this spec and wait for user approval:

```text
Indicator type:
Add mode:
Plot target:
Inputs:
Outputs / render keys:
Parameters:
Formula / algorithm:
Timeframe / session assumptions:
Context actions:
Drag / hit testing:
Per-series config / removal:
Persistence:
Kernel decision:
Plugin location:
Default indicator:
Test plan:
```

## Non-Negotiable Boundaries

- Chart code renders primitives only. Never add indicator-specific chart branches.
- Chart interactions emit generic coordinates/events only.
- Runtime routes generic capabilities only. Never parse indicator-specific series keys.
- Controller orchestrates UI flow only. Never branch on indicator names.
- Indicator behavior imports from `simplechart.api`.
- Extension packages register themselves:
  - `register_extension(YourIndicator)`
  - `register_store_handler(YourStoreHandler)` when persistent/runtime state is needed
- Store handlers live with the indicator package, not in `app/`.
- Numeric hot paths live in `_kernel.py` and take/return numpy arrays.
- Project plugins may be `.py` files or packages under `indicators/`. User
  plugins under `~/.simplechart/plugins/` are currently loaded as `.py` files.
- If an indicator requires changes outside its own package or module, stop and
  treat each app, chart, runtime, data, or public API change as a separate
  boundary-specific side task. Before editing those files, produce a short plan
  that names the boundary, explains why the indicator-only change is
  insufficient, lists the proposed files, identifies behavioral risk, and
  defines focused tests.

## Phase Gates

Proceed through these gates in order. Stop after each gate, explain what changed
or what decision was made, and wait for explicit user approval before continuing.

1. Requirements clarified
2. Domain research completed and summarized
3. Requirements spec approved
4. Conditional reference files read
5. Boundary and file plan approved
6. Indicator behavior implemented
7. Add capability implemented
8. Context actions implemented, if needed
9. Hit testing and drag behavior implemented, if needed
10. Per-series behavior implemented, if needed
11. Store handler implemented, if needed
12. Kernel implemented, if needed
13. Tests and verification completed

## Implementation Order

1. Define the indicator behavior.
   - Implement `name`, `label`, `default_params`, and `render` or `compute`.
   - Use stable render keys. Persistent objects need stable IDs in keys.
   - Prefer `render` when labels, colors, markers, handles, or per-object
     visual settings cannot be represented cleanly by the default compute path.
   - Use `ChartExtensionRender`, `SeriesRender`, and `MarkerRender`; add new
     primitives only when a real extension needs them.

2. Declare add capability.
   - `ChartExtensionAddMode.DIALOG` for normal config-dialog indicators.
   - `ChartExtensionAddMode.CONTEXT` for right-click chart actions.
   - `ChartExtensionAddMode.TOOLBAR` for drawing-mode tools.
   - `ChartExtensionAddMode.HIDDEN` for internal/helper extensions.

3. Add context actions if needed.
   - Implement `context_actions(...)`.
   - Implement `apply_action(...)` to return a `ChartExtensionMutation`.
   - Do not persist inside the indicator.

4. Add hit testing and drag behavior if needed.
   - Implement `hit_test(...)`, `begin_drag(...)`, `drag_to(...)`,
     `finish_drag(...)`, and `cancel_drag(...)`.
   - Drag preview returns render primitives only.
   - Finish/cancel returns mutations only.

5. Add per-series behavior if needed.
   - `toggles_series_independently(...)`
   - `config_for_series(...)`
   - `apply_config_to_series(...)`
   - `remove_series(...)`

6. Add a store handler only if state must persist or be injected at runtime.
   - Put it under the indicator package.
   - Register it from the indicator package.
   - It handles mutations, active-state creation, cleanup, and param injection.
   - Runtime and controller must not know the handler exists.

7. Decide whether a compiled kernel is needed.
   - Use a kernel for recurrence relations where `result[i]` depends on
     `result[i-1]`.
   - Use a kernel for long numeric per-bar loops that cannot be expressed clearly
     with numpy vector operations.
   - Do not use a kernel for vectorized numpy operations, existing project
     helpers, or rolling/windowed calculations that are already clear without a
     custom loop unless profiling shows the indicator is hot.
   - Do not create a kernel for interaction, persistence, key construction, or
     small render-array assembly.

8. Verify.
   - Add focused tests for render keys, context action, drag, config/remove, and store behavior.
   - Run:
     `python -m pytest`
     `python -m compileall app chart data indicators simplechart tools tests`
   - If you need a focused mypy check, ask the user before choosing the target,
     then run:
     `python -m mypy --follow-imports=skip path/to/touched_file.py`

## Anti-Patterns

- Importing Qt, finplot, chart, controller, or runtime from an indicator behavior file.
- Adding `if extension_name == ...` outside the extension package.
- Adding `if series_key.startswith(...)` outside the indicator package.
- Persisting directly from an indicator behavior method.
- Creating a generic abstraction before a real indicator needs it.
- Adding a chart primitive speculatively.

## Fibonacci Retracement Acceptance Bar

Use Fibonacci to validate the pattern:

- It registers as a plugin.
- It has no chart/controller/runtime branches.
- It uses generic render primitives for levels, labels, and handles. If a needed
  primitive is missing, add the smallest generic primitive required by the
  indicator rather than a Fibonacci-specific chart branch.
- It owns context or toolbar add mode.
- It owns hit testing and drag sessions.
- Any persistence lives in a registered store handler under its package.
