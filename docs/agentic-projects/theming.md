# Theming (Dark Mode) — spec v1.0

Approved by the user 2026-11-18. Tier: Large (S1 engine → S2 migration/wiring → one Opus review → screenshots).

## Objective

Replace the disabled "Dark Mode" checkbox with theme files, so the app can switch between Light and Dark and users can add their own color schemes. Model: Textual's theme (base colors → derived shades → `$variables` in a stylesheet) plus Rich-style files with inheritance.

## Decisions (user-approved)

- Settings → UI: the "Dark Mode" checkbox becomes a **Theme** dropdown listing built-in and user themes. The change applies **on next launch**.
- Built-in themes: **Light** (exactly today's colors; no visual change) and **Dark** (Webull-style dark). The label is "Dark", never "Webull Dark".
- Indicator and drawing colors are untouched; themes do not set indicator defaults.
- No new dependency: TOML via stdlib `tomllib`; `$var` substitution via `string.Template`.

## Theme files

- Built-ins: `app/themes/light.toml`, `app/themes/dark.toml` (shipped as package data).
- User themes: `~/.simplechart/themes/*.toml`.
- Schema:
  ```toml
  name = "My Theme"        # required, shown in the dropdown
  dark = true              # required unless inherited
  inherits = "dark"        # optional; built-in names only ("light" | "dark")
  [colors]                 # base colors; any key the default theme defines
  background = "#16181d"
  [variables]              # optional; overrides any derived value by name
  primary_darken_1 = "#1e4fd8"
  ```
- Colors are `#rrggbb` or `#rrggbbaa`. Keys are `[a-z][a-z0-9_]*`.
- A user theme only lists what it changes. Missing keys come from its `inherits` parent, or from Light if it has none.
- The key set is owned by `light.toml`. A key that Light doesn't define is an error. Built-in Dark must define every key Light defines.

## Derivation (computed at load time)

For every base color `k`:
- `k_lighten_1..3` and `k_darken_1..3`: HSL lightness ± 0.075 per step, clamped; alpha kept.
- `k_text`: `#ffffff` or `#000000`, whichever contrasts more (WCAG relative luminance).
- Plus `foreground_muted`: foreground blended 60 % over background.
- `[variables]` overrides are applied last and must name an existing derived or base key.

## Failure handling (system boundary)

- An invalid user file (TOML error, bad color, unknown key, bad `inherits`, a duplicate or built-in name) is skipped. The reason is collected as a notice shown once at startup; the app never crashes.
- If the saved theme name no longer exists, use Light and show a notice.

## Consumers of the one resolved color dict

1. One app-wide Qt stylesheet template (`$name` substitution) replaces the inline `setStyleSheet` color literals in app widgets.
2. Chart colors (finplot globals, candles, volume, grid, axis text, crosshair, hollow-bull body) come in through a typed chart-colors object passed from the app layer. The chart layer must not import `app`.
3. A `QPalette` (Fusion style) for native widgets and dialogs.

## Units

- **S1 (engine)**, Sonnet: `app/theme.py` (load, validate, inherit, derive, list, render the template) + `app/themes/light.toml` with a starter key set + unit tests. No widget changes.
- **S2 (migration and wiring)**, Sonnet: finish the key set from the inventory of real inline colors; write `dark.toml`; QSS template; chart-colors injection; QPalette; settings storage (`application_settings.theme`, default "Light"); the Theme dropdown; startup notices; update tests that assert light literals.
- **Review**, Opus: one pass over S1+S2.
- **Verify** (orchestrator): screenshots in Light and Dark; full suite, ruff and mypy.

## Acceptance

- In Light, the app looks identical to before. The existing color-literal tests pass unchanged, or with only an import swap.
- Dark renders the chart, header, symbol bar, watchlist, window chrome, legend and dialogs in dark colors with readable text.
- A user theme file in `~/.simplechart/themes/` shows up in the dropdown, and choosing it takes effect on restart. A broken one produces a notice and doesn't crash the app.
- Production call site: app startup (`MainWindow`) applies the saved theme. An integration test proves a non-default theme reaches the stylesheet and the chart.

## Log

(evidence and adjustments go here)

### S2 (migration and wiring)

- Files: `app/themes/{light.toml,dark.toml,app.qss}`, `app/theme.py` (active theme, `app_stylesheet`, `apply_theme`, QPalette), `app/controller.py` (`_start_theme`, `_chart_colors`, `USER_THEMES_DIR`, appFrame via `customChrome` property), `app/{header_bar,symbol_bar,watchlist,window_chrome,data_quality,extension_config,application_settings}.py`, `chart/{styles,window,plot_manager,legend}.py` (`ChartColors`), `data/cache.py` (`get_theme`/`set_theme`), `pyproject.toml` (`themes/*.qss`), `tests/conftest.py` (autouse `_theme_state_restored`), new `tests/test_theme_app.py`.
- Key set: 16 S1 keys revised (`success` #198754, `warning` #d39e00, `grid` #3333330f, `crosshair` #00000077; `accent` kept, it matches the refresh-done banner) plus about 50 added so every literal has an exact Light value. Dark defines all of them.
- Static widget styles live in `app.qss`; state-dependent ones (source dot, percent labels, refresh banner, header HTML, legend hidden color, chrome icons, swatch border) stay inline and read `active_colors()`.
- Light pixel-diff: 0 differing pixels for the main window and for a widget sheet (title bar, header, symbol bar, watchlist, Tools palette, Add Symbol popup).
- Adjustments: `grid` only contributes its alpha (pyqtgraph draws grid lines in the axis color); `crosshair` was finplot's default `#0007`, now explicit. `tests/test_no_raw_exception_output.py` allowlists `app/theme.load_themes` (the notice text for a user's own theme file). S1 tests that expected only `["Light"]` now expect Light and Dark. `ChartColors` also carries `legend_hidden`.

### QC fixes

- Deeply nested user TOML raised `RecursionError` out of `load_themes`; it is now skipped with a notice.
- User theme names are rejected when they match an existing name case-insensitively or contain control characters; `inherits` is lower-cased, so `inherits = "Light"` works.
- `_palette` also sets Light, Midlight, Mid, Dark, Shadow, Link and LinkVisited from theme keys.
- The Settings theme combo saved Light over a saved name that was missing from the list; `set_theme` now runs only when the combo text changed.
- New theme key `last_price` (Light #0000ff, Dark #2962ff) travels in `ChartColors`; the line (alpha 80) and tag (alpha b3) use it. Light pixels are unchanged.
- The shared notice dialog is titled "Startup Notice" (was "Extension Problem").
- `apply_theme` sets the stylesheet only when it differs from the current one (about 7 % of suite time); the test fixture restores the stylesheet whenever it changed.
- Tests: the Dark window test now checks `active_colors()`, an inline consumer and Fusion from a non-Fusion start (mutations dropping `_active = theme` and `setStyle("Fusion")` both fail it); the window-chrome test checks the application's stylesheet and the title bar's object name.
- Reviewer verdicts: keep the allowlist for `app/theme.load_themes` in the raw-exception test (5a) and keep the idempotent stylesheet guard (5b).
- Deliberate change from the spec's ChartColors-only wording: the chart's own widgets (legend buttons, Tools palette) are styled by object name from `app.qss`.
