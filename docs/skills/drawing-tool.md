# Drawing Tool Skill

Use this when creating or refactoring a SimpleChart **drawing tool** — an
interactive overlay the user places and manipulates directly on the chart
(vertical line, Fibonacci retracement).

A tool is a `ChartExtension` with a TOOLBAR (or CONTEXT) add mode, on-chart
drawing/drag interaction, and a `DrawingStore`-backed persistence handler for
the user-placed records.

Use a different skill when the work is not a drawing tool:

- `docs/skills/compute-indicator.md` — indicators that convert OHLCV into arrays
  (SMA, EMA, RSI, pivots).
- `docs/skills/interactive-indicator.md` — interactive *indicators* that compute
  from data but add anchors/handles (AVWAP). AVWAP is an indicator, not a tool:
  it persists on both axes always and is added via CONTEXT, not the toolbar.

Read this entire file before writing any code.

**Core rule — extensions are stateless.** Never store mutable state on `self`;
all state flows through `params`, the event/session objects, and your
`DrawingStore`. The framework constructs, discards, and may share instances
freely, so `self`-held state would bleed across drawings, symbols, and
timeframes.

---

## Read First

- `simplechart/api.py` — the public API; every import in a tool comes from here.
- `simplechart/extensions/_base.py` — the ChartExtension ABC, render primitives,
  drawing/drag/hit-test hooks, and the store-handler + store-context protocols.
- `simplechart/extensions/_drawing_store.py` — the `DrawingStore` base and
  `AxisPolicy`; the persistence framework every tool uses.
- `app/extension_runtime.py` — generic event/drawing/drag/config/remove routing.
- `app/extension_store.py` — the store-handler boundary and app store context.

Reference tools — model new tools on these:

- `tools/vertical_line/` — minimal one-click tool; both persistence axes `USER`,
  with two Configure toggles.
- `tools/fib_retracement/` — two-anchor click-drag-out tool; both axes
  `FIXED_OFF` (timeframe-scoped, volatile); per-level segments with handles and
  labels.

---

## Persistence model — read before specifying

Every tool declares, per axis, one `AxisPolicy`:

| Policy | Meaning |
|--------|---------|
| `FIXED_ON` | Always persists on that axis; no toggle. |
| `FIXED_OFF` | Never persists on that axis; no toggle. |
| `USER` | The per-drawing choice decides; surfaced as a Configure toggle. |

The two independent axes:

- **timeframe** — shown on every timeframe, or pinned to the timeframe the
  drawing was created on (hidden elsewhere, reappears on return).
- **session** — durable (survives restart via the store context's SQLite-backed
  records) or volatile (process memory; gone on restart, but survives
  symbol/timeframe switches within a run).

`DrawingStore` handles all of it: SQLite-vs-memory routing, timeframe
tagging/filtering, id assignment (durable records get the positive SQLite row
id; volatile records get a negative id), and ensuring/removing the tool's
extension state as drawings appear and disappear on the current timeframe. The
tool only declares the two policies and implements the record hooks.

For a `USER` axis, store the per-drawing choice as a bool field on the record and
expose it as a bool param in `config_for_series` (it renders as a checkbox); the
base reads it via `wants_timeframe_persistence` / `wants_session_persistence`.

---

## Research

Research the tool's accepted definition, interaction model, and any geometry
(anchors, levels, price/time math). Cite primary or authoritative references.
Summarize for the user — what it draws, the exact interaction rules, the chart
coordinates involved, timeframe/session assumptions, and any ambiguities — and
wait for approval before finalizing the spec. Do not add a dependency or
copy/adapt code without explicit approval.

---

## Tool spec — produce and get approval before editing

```text
Tool name / label:
Add mode:                 TOOLBAR | CONTEXT
Drawing interaction:      one-click commit | click-drag-out two anchors | ...
Record model:             frozen dataclass fields (incl. timeframe, updated_at_ms, age_off_days, and any USER flags)
Render keys:              stable per-drawing key scheme
Render primitives:        segments / vertical lines / markers used
Hit test / drag:          handles and what each drag adjusts
Config / removal:         Configure params (incl. persistence toggles for USER axes)
Persistence:              timeframe axis = FIXED_ON | FIXED_OFF | USER
                          session axis   = FIXED_ON | FIXED_OFF | USER
                          defaults for any USER axis
Test plan:
```

---

## Non-Negotiable Boundaries

- Chart code renders primitives only. Never add tool-specific chart branches.
- Chart interactions emit generic coordinates/events only.
- Runtime routes generic capabilities only. Never parse tool-specific keys.
- Controller orchestrates UI flow only. Never branch on extension names.
- Tool behavior imports from `simplechart.api`.
- The tool registers itself:
  - `register_extension(YourTool)`
  - `register_store_handler(YourStore)`
- The store handler lives in the tool package, subclassing `DrawingStore`.
- If a tool needs a change outside its own package (app, chart, runtime, data,
  public API), stop and treat each as a separate boundary-specific side task:
  name the boundary, explain why the tool-only change is insufficient, list the
  files, identify behavioral risk, and define focused tests before editing.

---

## Phase Gates

Proceed in order. Stop after each gate, explain what changed, and wait for
explicit user approval before continuing.

1. Requirements clarified
2. Domain research completed and summarized
3. Spec approved
4. Reference files read
5. File plan approved
6. Record model + tool behavior implemented
7. Drawing interaction implemented
8. Hit testing and drag implemented
9. Config / removal implemented
10. DrawingStore subclass implemented
11. Tests and verification completed

---

## Implementation Order

1. **Record model** — `tools/<tool>/models.py`, a frozen dataclass. Include
   `symbol`, the anchor timestamp(s), `timeframe: str` (the creation timeframe),
   `updated_at_ms: int = 0`, `age_off_days: float = <tool default>`,
   an id field (`<thing>_id: int | None = None`), and one bool field per `USER`
   axis (e.g. `persist_across_timeframes: bool = True`). `updated_at_ms` is
   stamped by `DrawingStore`; `age_off_days=0` means never expire.

2. **Tool behavior** — `tools/<tool>/__init__.py`, a `ChartExtension`:
   - `name` / `label` / `default_params` / `add_mode` (TOOLBAR or CONTEXT) /
     `preserve_ui_state_per_symbol` (usually `False` — the store is the source of
     truth).
   - `compute()` returns `{}`; tools render, they do not compute arrays.
   - `render()` builds primitives from the injected records
     (`params[params_key]`). Use stable per-drawing render keys.
   - Set `reference=True` on any SeriesRender/HorizontalSegmentRender that must
     stay out of the legend (see "Legend exclusion" below).

3. **Drawing interaction**:
   - One-click: `start_drawing` returns a `DrawingToolResult` with an `add`
     mutation, `done=True`, `deactivate_tool=True`.
   - Click-drag-out: `start_drawing` opens a `DrawingSession`; `preview_drawing`
     returns a render preview each move; `advance_drawing` commits via the `add`
     mutation; `cancel_drawing` returns `None`.
   - Build the record fully (`symbol`, `timeframe=series.timeframe.value`,
     defaults for any `USER` flags); leave the id `None` — the store assigns it.

4. **Hit test + drag** — `hit_test`, `begin_drag`, `drag_to`, `finish_drag`,
   `cancel_drag`:
   - `drag_to` returns a render preview only (no mutation).
   - `finish_drag` returns an `update` mutation.
   - `cancel_drag` returns `None` — dragging only previews; the store is never
     touched mid-drag, and the controller re-renders from store state on cancel.

5. **Config / removal**:
   - `config_for_series` returns a `ChartExtensionConfig`. Include
     `"age_off_days": FloatParam(record.age_off_days, minimum=0.0, ...)`.
     For each `USER` axis, add a bool param (e.g.
     `"persist_across_timeframes"`) — the config dialog renders it as a checkbox
     with a Title-Case label.
   - `apply_config_to_series` returns an `update` mutation carrying the edited
     record; read the persistence flags from `edited_params`.
   - `remove_series` returns a `delete` mutation.

6. **DrawingStore subclass** — `tools/<tool>/session_store.py`:
   - Set `extension_name`, `store_key`, `params_key`, `timeframe_axis`,
     `session_axis`.
   - Implement `to_payload`, `from_payload`, `sort_key`, `record_id`, `with_id`,
     `series_key`, `created_timeframe`, `updated_at_ms`, `age_off_days`, and
     `touch_record`.
   - Store `updated_at_ms` and `age_off_days` in the payload. For legacy payloads
     that lack them, read `updated_at_ms` as `0` and `age_off_days` as the tool
     default; `DrawingStore` will stamp and backfill on load.
   - For a `USER` axis, override `wants_timeframe_persistence` /
     `wants_session_persistence` to read the record's flag.
   - Keep the series-key function in this module (the tool imports it).

7. **Register** — at the bottom of `__init__.py`:
   `register_extension(YourTool)` and `register_store_handler(YourStore)`.

---

## Mutation vocabulary (exact)

`DrawingStore` routes exactly three operations; the tool emits only these:

| operation | payload | when |
|-----------|---------|------|
| `add` | `{"record": R}` | new drawing (id unset; the store assigns it) |
| `update` | `{"record": R}` | move/reconfigure (id set; the store re-routes if a `USER` session toggle flipped durability) |
| `delete` | `{"record": R}` | remove (id set) |

Do not invent other operation names; the base only routes these. There is no
`restore` — cancel paths return `None` and rely on the controller re-rendering
from store state.

---

## Legend exclusion

To keep a tool's render keys out of the legend, set `reference=True` on its
`SeriesRender` / `HorizontalSegmentRender`. A tool managed entirely on-chart
(e.g. fib retracement) sets `reference=True` on **all** segments so no level
appears in the legend. Do not rely on a substring in the key — that implicit
contract no longer exists.

---

## Verify

- `python -m pytest`
- `python -m compileall app chart indicators simplechart tools tests`
- Launch the app, draw the tool, then exercise the declared persistence:
  switch timeframes (does it show/hide as declared, and reappear on return?) and
  restart the app (does it survive only if session-durable?). For a `USER` axis,
  flip the Configure toggle and re-check.
- If you need a focused mypy check, ask the user before choosing the target, then
  run `python -m mypy --follow-imports=skip path/to/touched_file.py`.

---

## Anti-Patterns

- Importing Qt, finplot, chart, controller, or runtime from a tool file.
- Adding `if extension_name == ...` outside the tool package.
- Adding `if series_key.startswith(...)` outside the tool package.
- Persisting directly from a tool method instead of through the store.
- Inventing mutation operation names beyond add / update / delete.
- Storing persistence metadata (timeframe tag, durability) anywhere but the
  record — the store derives everything from the record and the declared policy.
- Relying on a `_ref_` substring for legend exclusion.
- Creating a generic abstraction before a real tool needs it.

---

## Acceptance Bar

Validate a new tool against `vertical_line` and `fib_retracement`:

- Registers as a plugin (`register_extension` + `register_store_handler`).
- No chart/controller/runtime branches; renders via generic primitives.
- Owns its TOOLBAR/CONTEXT add mode, drawing interaction, and hit test + drag.
- Persists via a `DrawingStore` subclass that declares an `AxisPolicy` per axis;
  any `USER` axis exposes a Configure toggle backed by a record field.
- Sets `reference=True` on segments that must stay out of the legend.
