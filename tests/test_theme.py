from pathlib import Path

import pytest

from app.theme import Theme, load_themes, render_stylesheet


def _write(tmp_path: Path, text: str, name: str = "t.toml") -> None:
    (tmp_path / name).write_text(text, encoding="utf-8")


def _load(tmp_path: Path) -> tuple[dict[str, Theme], list[str]]:
    return load_themes(tmp_path)


def test_light_loads() -> None:
    themes, notices = load_themes(Path("/nonexistent-theme-dir"))
    light = themes["Light"]
    assert list(themes)[0] == "Light"
    assert not light.dark
    assert light.colors["background"] == "#ffffff"
    assert light.colors["volume_up"] == "#00c85366"
    assert notices == []


def test_derived_keys_exist() -> None:
    colors = load_themes(Path("/nonexistent-theme-dir"))[0]["Light"].colors
    for suffix in ("lighten_1", "lighten_3", "darken_1", "darken_3", "text"):
        assert f"primary_{suffix}" in colors
    assert "foreground_muted" in colors


def test_derivation_steps_lightness(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "G"\n[colors]\nprimary = "#808080"\n')
    colors = _load(tmp_path)[0]["G"].colors
    assert colors["primary_darken_1"] == "#6d6d6d"
    assert colors["primary_lighten_1"] == "#939393"


def test_derivation_keeps_alpha(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "A"\n[colors]\nprimary = "#80808040"\n')
    colors = _load(tmp_path)[0]["A"].colors
    assert colors["primary_lighten_2"].endswith("40")
    assert len(colors["primary_lighten_2"]) == 9


def test_lightness_clamps(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "C"\n[colors]\nprimary = "#ffffff"\nerror = "#000000"\n')
    colors = _load(tmp_path)[0]["C"].colors
    assert colors["primary_lighten_3"] == "#ffffff"
    assert colors["error_darken_3"] == "#000000"


def test_contrast_text(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "T"\n[colors]\nprimary = "#000000"\nerror = "#ffffff"\n')
    colors = _load(tmp_path)[0]["T"].colors
    assert colors["primary_text"] == "#ffffff"
    assert colors["error_text"] == "#000000"


def test_foreground_muted_blend(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "M"\n[colors]\nbackground = "#000000"\nforeground = "#ffffff"\n')
    assert _load(tmp_path)[0]["M"].colors["foreground_muted"] == "#999999"


def test_user_theme_inherits_light_and_overrides(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "Mine"\n[colors]\nprimary = "#112233"\n')
    themes, notices = _load(tmp_path)
    light, mine = themes["Light"], themes["Mine"]
    assert notices == []
    assert mine.colors["primary"] == "#112233"
    assert mine.colors["background"] == light.colors["background"]
    assert mine.dark == light.dark
    assert list(themes)[-1] == "Mine"


def test_variables_override_derived(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "V"\n[variables]\nprimary_darken_1 = "#1E4FD8"\n')
    assert _load(tmp_path)[0]["V"].colors["primary_darken_1"] == "#1e4fd8"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('name = "X"\n[colors\n', ""),
        ('name = "X"\n[colors]\nprimary = "blue"\n', "#rrggbb"),
        ('name = "X"\n[colors]\nnope = "#000000"\n', "unknown color key"),
        ('name = "X"\ninherits = "ghost"\n', "inherits"),
        ('[colors]\nprimary = "#000000"\n', "missing name"),
        ('name = "Light"\n', "duplicate"),
        ('name = "X"\n[variables]\nnope_darken_1 = "#000000"\n', "unknown variable"),
    ],
)
def test_invalid_user_theme_skipped_with_notice(tmp_path: Path, text: str, reason: str) -> None:
    _write(tmp_path, text, "bad.toml")
    themes, notices = _load(tmp_path)
    assert list(themes) == ["Light", "Dark"]
    assert len(notices) == 1
    assert "bad.toml" in notices[0]
    assert reason in notices[0]


def test_duplicate_user_names_skip_second(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "Same"\n', "a.toml")
    _write(tmp_path, 'name = "Same"\n', "b.toml")
    themes, notices = _load(tmp_path)
    assert "Same" in themes
    assert len(notices) == 1
    assert "b.toml" in notices[0]


def test_bad_file_does_not_block_good_one(tmp_path: Path) -> None:
    _write(tmp_path, "garbage = [", "a.toml")
    _write(tmp_path, 'name = "Good"\n', "b.toml")
    themes, notices = _load(tmp_path)
    assert "Good" in themes
    assert len(notices) == 1


def test_missing_user_dir_is_no_user_themes(tmp_path: Path) -> None:
    themes, notices = load_themes(tmp_path / "missing")
    assert list(themes) == ["Light", "Dark"]
    assert notices == []


def test_render_stylesheet_substitutes() -> None:
    theme = load_themes(Path("/nonexistent-theme-dir"))[0]["Light"]
    out = render_stylesheet("QWidget { background: $background; color: $foreground; }", theme)
    assert out == "QWidget { background: #ffffff; color: #333333; }"


def test_render_stylesheet_unknown_variable_raises() -> None:
    theme = load_themes(Path("/nonexistent-theme-dir"))[0]["Light"]
    with pytest.raises(KeyError):
        render_stylesheet("color: $nope;", theme)


def test_deeply_nested_file_is_skipped_with_notice(tmp_path: Path) -> None:
    _write(tmp_path, "x = " + "[" * 5000, "deep.toml")
    themes, notices = _load(tmp_path)
    assert list(themes) == ["Light", "Dark"]
    assert len(notices) == 1
    assert "deep.toml" in notices[0]


@pytest.mark.parametrize("name", ["light", "DARK", "bad\\nname", "bad\\x07name"])
def test_user_theme_name_clash_or_control_character_is_skipped(tmp_path: Path, name: str) -> None:
    _write(tmp_path, f'name = "{name}"\n', "n.toml")
    themes, notices = _load(tmp_path)
    assert list(themes) == ["Light", "Dark"]
    assert len(notices) == 1


def test_inherits_is_case_insensitive(tmp_path: Path) -> None:
    _write(tmp_path, 'name = "Mine"\ninherits = "Dark"\n')
    themes, notices = _load(tmp_path)
    assert notices == []
    assert themes["Mine"].dark
