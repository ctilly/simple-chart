"""Theme engine: load, validate, inherit and derive colors; render stylesheets."""

import colorsys
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

_COLOR = re.compile(r"#[0-9a-f]{6}(?:[0-9a-f]{2})?")
_KEY = re.compile(r"[a-z][a-z0-9_]*")
_STEP = 0.075
_BUILTIN_DIR = Path(__file__).parent / "themes"


@dataclass(frozen=True)
class Theme:
    name: str
    dark: bool
    colors: dict[str, str]


@dataclass(frozen=True)
class _Spec:
    name: str
    dark: bool
    base: dict[str, str]
    variables: dict[str, str]


def _color(value: object, where: str) -> str:
    text = value.lower() if isinstance(value, str) else ""
    if not _COLOR.fullmatch(text):
        raise ValueError(f"{where}: {value!r} is not #rrggbb or #rrggbbaa")
    return text


def _table(raw: dict[str, Any], section: str) -> dict[str, str]:
    table = raw.get(section, {})
    if not isinstance(table, dict):
        raise ValueError(f"[{section}] must be a table")
    out: dict[str, str] = {}
    for key, value in table.items():
        if not _KEY.fullmatch(key):
            raise ValueError(f"[{section}] invalid key {key!r}")
        out[key] = _color(value, f"{section}.{key}")
    return out


def _parse(text: str, parent: _Spec | None, light: _Spec | None) -> _Spec:
    """Parse one theme file. ``parent`` supplies inherited values; ``light`` owns the key set."""
    raw = tomllib.loads(text)
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("missing name")
    dark = raw.get("dark", parent.dark if parent else None)
    if not isinstance(dark, bool):
        raise ValueError("dark must be true or false")
    colors = _table(raw, "colors")
    variables = _table(raw, "variables")
    if light is not None:
        unknown = sorted(set(colors) - set(light.base))
        if unknown:
            raise ValueError(f"unknown color key(s): {', '.join(unknown)}")
    base = {**parent.base, **colors} if parent else colors
    if light is not None:
        missing = sorted(set(light.base) - set(base))
        if missing:
            raise ValueError(f"missing color key(s): {', '.join(missing)}")
    return _Spec(name.strip(), dark, base, {**parent.variables, **variables} if parent else variables)


def _rgb(color: str) -> tuple[float, float, float]:
    return (int(color[1:3], 16) / 255, int(color[3:5], 16) / 255, int(color[5:7], 16) / 255)


def _hex(r: float, g: float, b: float) -> str:
    return "#" + "".join(f"{round(c * 255):02x}" for c in (r, g, b))


def _luminance(color: str) -> float:
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _resolve(spec: _Spec) -> Theme:
    colors = dict(spec.base)
    for key, value in spec.base.items():
        h, lightness, s = colorsys.rgb_to_hls(*_rgb(value))
        alpha = value[7:]
        for step in (1, 2, 3):
            up = min(1.0, lightness + _STEP * step)
            down = max(0.0, lightness - _STEP * step)
            colors[f"{key}_lighten_{step}"] = _hex(*colorsys.hls_to_rgb(h, up, s)) + alpha
            colors[f"{key}_darken_{step}"] = _hex(*colorsys.hls_to_rgb(h, down, s)) + alpha
        lum = _luminance(value)
        colors[f"{key}_text"] = "#ffffff" if 1.05 / (lum + 0.05) > (lum + 0.05) / 0.05 else "#000000"
    fg, bg = _rgb(spec.base["foreground"]), _rgb(spec.base["background"])
    colors["foreground_muted"] = _hex(*(0.6 * f + 0.4 * b for f, b in zip(fg, bg, strict=True)))
    for key, value in spec.variables.items():
        if key not in colors:
            raise ValueError(f"unknown variable {key!r}")
        colors[key] = value
    return Theme(spec.name, spec.dark, colors)


def _builtin_specs() -> dict[str, _Spec]:
    files = sorted(_BUILTIN_DIR.glob("*.toml"), key=lambda p: (p.stem != "light", p.stem))
    specs: dict[str, _Spec] = {}
    for path in files:
        specs[path.stem] = _parse(path.read_text(encoding="utf-8"), None, specs.get("light"))
    return specs


def load_themes(user_dir: Path) -> tuple[dict[str, Theme], list[str]]:
    """Return themes by display name (built-ins first) and notices for skipped user files."""
    specs = _builtin_specs()
    themes = {spec.name: _resolve(spec) for spec in specs.values()}
    notices: list[str] = []
    for path in sorted(user_dir.glob("*.toml")):
        try:
            text = path.read_text(encoding="utf-8")
            inherits = tomllib.loads(text).get("inherits", "light")
            if isinstance(inherits, str):
                inherits = inherits.lower()
            if not isinstance(inherits, str) or inherits not in specs:
                raise ValueError(f"unknown inherits {inherits!r}")
            spec = _parse(text, specs[inherits], specs["light"])
            if not spec.name.isprintable():
                raise ValueError(f"invalid theme name {spec.name!r}")
            if spec.name.lower() in {name.lower() for name in themes}:
                raise ValueError(f"duplicate theme name {spec.name!r}")
            themes[spec.name] = _resolve(spec)
        except (OSError, ValueError, RecursionError) as exc:
            notices.append(f"Theme file {path.name} skipped: {exc}")
    return themes, notices


def render_stylesheet(template: str, theme: Theme) -> str:
    return Template(template).substitute(theme.colors)


_active: Theme | None = None


def active_theme() -> Theme:
    """The theme the application was started with; Light until one is applied."""
    global _active
    if _active is None:
        _active = _resolve(_builtin_specs()["light"])
    return _active


def active_colors() -> dict[str, str]:
    return active_theme().colors


def app_stylesheet(theme: Theme) -> str:
    template = (_BUILTIN_DIR / "app.qss").read_text(encoding="utf-8")
    return render_stylesheet(template, theme)


def _palette(theme: Theme) -> QPalette:
    c = theme.colors
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: c["background"],
        QPalette.ColorRole.WindowText: c["foreground"],
        QPalette.ColorRole.Base: c["surface"],
        QPalette.ColorRole.AlternateBase: c["panel"],
        QPalette.ColorRole.Text: c["foreground"],
        QPalette.ColorRole.Button: c["panel"],
        QPalette.ColorRole.ButtonText: c["foreground"],
        QPalette.ColorRole.Highlight: c["primary"],
        QPalette.ColorRole.HighlightedText: c["primary_text"],
        QPalette.ColorRole.ToolTipBase: c["raised"],
        QPalette.ColorRole.ToolTipText: c["foreground"],
        QPalette.ColorRole.PlaceholderText: c["text_secondary"],
        QPalette.ColorRole.Light: c["raised_lighten_2"],
        QPalette.ColorRole.Midlight: c["raised_lighten_1"],
        QPalette.ColorRole.Mid: c["border"],
        QPalette.ColorRole.Dark: c["border_darken_2"],
        QPalette.ColorRole.Shadow: c["background_darken_3"],
        QPalette.ColorRole.Link: c["primary_lighten_2"],
        QPalette.ColorRole.LinkVisited: c["primary"],
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["text_disabled"]))
    return palette


def apply_theme(app: QApplication, theme: Theme) -> None:
    """Make theme the application's: stylesheet, and for dark themes Fusion plus a palette.

    Light leaves the platform style and palette alone so it looks as it always has.
    """
    global _active
    _active = theme
    if theme.dark:
        app.setStyle("Fusion")
        app.setPalette(_palette(theme))
    sheet = app_stylesheet(theme)
    if app.styleSheet() != sheet:
        app.setStyleSheet(sheet)
