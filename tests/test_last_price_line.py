"""
Last-price line: a subtle blue line from the newest candle's close to the
price axis, with a small tag on the axis (docs/agentic-projects/
last-price-line.md). Uses the live-refresh harness: a Monday session clock
and a source whose newest bar is still forming.
"""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyqtgraph as pg
import pytest
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QFont, QFontInfo, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog

import app.controller as controller_module
from app.application_settings import ApplicationSettingsDialog
from app.controller import INITIAL_EXTENSIONS, MainWindow
from chart.plot_manager import PlotManager
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
from simplechart.api import AxisPriceLabelRender
from test_basis_change import (  # noqa: F401  (fixture)
    _SYMBOL,
    _WEDNESDAY,
    _WEDNESDAY_CLOSE,
    _bars,
    _isolated_finplot_windows,
    _seed,
    _settled,
    _show,
    _window,
)
from test_live_refresh import (  # noqa: F401  (fixtures)
    _LiveSource,
    _at,
    _clock,
    _live_polling,
    _live_window,
    _poll,
    _forming_close,
)
from provider_support import startup_registry

_DEFAULT_EXTENSIONS = list(INITIAL_EXTENSIONS)


class _EmptyableSource(_LiveSource):
    def __init__(self) -> None:
        super().__init__()
        self.empty = False

    def fetch_bars(self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
        bars = super().fetch_bars(symbol, timeframe, start, end)
        return [] if self.empty else bars


@pytest.fixture
def source() -> _EmptyableSource:
    return _EmptyableSource()


def _pm(window: MainWindow) -> PlotManager:
    return window._chart.plot_manager


def _view(window: MainWindow) -> Any:
    return _pm(window).price_viewbox()


def _line(window: MainWindow) -> tuple[list[float], list[float]] | None:
    item = _pm(window)._last_price_line
    if item is None:
        return None
    x, y = item.getData()
    return [float(v) for v in x], [float(v) for v in y]


def _tag(window: MainWindow) -> Any:
    handle = _pm(window)._last_price_label
    return None if handle is None else handle.text_item


def _assert_marker_at(window: MainWindow, bars: list[Bar]) -> None:
    line = _line(window)
    assert line is not None
    xs, ys = line
    assert xs[0] == len(bars) - 1
    assert ys == [bars[-1].close, bars[-1].close]
    tag = _tag(window)
    assert tag is not None
    assert tag.textItem.toPlainText() == f"{bars[-1].close:.2f}"


def _assert_no_marker(window: MainWindow) -> None:
    assert _pm(window)._last_price_line is None
    assert _pm(window)._last_price_label is None


# ----------------------------------------------------------------------
# AC1, AC13: what is drawn
# ----------------------------------------------------------------------


def test_after_a_load_the_line_and_tag_sit_at_the_newest_close(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)

    _assert_marker_at(window, shown)
    viewbox = _view(window)
    line = _line(window)
    assert line is not None
    assert line[0][-1] == pytest.approx(viewbox.viewRect().right())
    tag = _tag(window)
    assert tag.scene() is viewbox.scene()
    assert tag.isVisible()
    assert tag.pos().x() == pytest.approx(viewbox.sceneBoundingRect().right())
    assert tag.color.name() == "#ffffff"
    line_item: Any = _pm(window)._last_price_line
    assert line_item.opts["pen"].color().name() == "#0000ff"


def _rendered(window: MainWindow, item: Any) -> QImage:
    """The scene pixels under item: the tag over the price axis."""
    rect: QRectF = item.sceneBoundingRect()
    image = QImage(round(rect.width()), round(rect.height()), QImage.Format.Format_ARGB32)
    # pg.TextItem resets the painter to its scene transform the first time
    # it paints, so the first render is only a warm-up.
    for _ in range(2):
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        _view(window).scene().render(painter, QRectF(image.rect()), rect)
        painter.end()
    return image


def _is_tag_blue(color: QColor) -> bool:
    # #0000ff at 70% over white.
    return color.blue() > 240 and color.red() < 110 and color.green() < 110


def test_the_tag_is_blue_with_rounded_corners(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)

    image = _rendered(window, _tag(window))

    # The padding rows above and below the text are filled to the middle.
    middle = image.width() // 2
    assert _is_tag_blue(image.pixelColor(middle, 1))
    assert _is_tag_blue(image.pixelColor(middle, image.height() - 2))
    for x, y in [(0, 0), (image.width() - 1, 0), (0, image.height() - 1)]:
        assert not _is_tag_blue(image.pixelColor(x, y))


def test_the_first_paint_of_the_tag_in_the_chart_view_is_right(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    tag = _tag(window)
    [view] = _view(window).scene().views()

    image = view.grab().toImage()

    # Placement only (the corner shape is pinned by the scene render
    # above): the tag sits on half pixels here, so its edges are blended.
    rect = view.mapFromScene(tag.sceneBoundingRect()).boundingRect()
    middle = rect.left() + rect.width() // 2
    assert _is_tag_blue(image.pixelColor(middle, rect.top() + 1))
    assert _is_tag_blue(image.pixelColor(middle, rect.bottom() - 1))
    assert not _is_tag_blue(image.pixelColor(middle, rect.top() - 2))
    assert not _is_tag_blue(image.pixelColor(middle, rect.bottom() + 2))


def test_the_tag_is_smaller_and_subtler_than_a_horizontal_line_tag(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    pm = _pm(window)
    pm.update_axis_price_label(
        AxisPriceLabelRender("h", shown[-1].close, "1.00", "#ff9800", "#ffffff"),
    )
    horizontal = pm._axis_price_labels["h"].text_item

    tag = _tag(window)
    tag_height = tag.sceneBoundingRect().height()
    horizontal_height = horizontal.sceneBoundingRect().height()  # type: ignore[attr-defined]
    assert tag_height <= 0.7 * horizontal_height
    assert tag.zValue() > horizontal.zValue()  # type: ignore[attr-defined]


def test_the_tag_is_smaller_with_a_pixel_sized_system_font(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    original = app.font()
    pixel_font = QFont(original)
    pixel_font.setPixelSize(16)
    app.setFont(pixel_font)
    try:
        window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
        pm = _pm(window)
        pm.update_axis_price_label(
            AxisPriceLabelRender("h", shown[-1].close, "1.00", "#ff9800", "#ffffff"),
        )
        horizontal: Any = pm._axis_price_labels["h"].text_item

        tag_size = QFontInfo(_tag(window).textItem.font()).pointSizeF()
        horizontal_size = QFontInfo(horizontal.textItem.font()).pointSizeF()
        assert 0 < tag_size < horizontal_size
    finally:
        app.setFont(original)


def test_the_line_stays_behind_indicator_lines_across_redraws(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window = _window(qtbot, tmp_path / "test.db", monkeypatch, source)
    monkeypatch.setattr(controller_module, "INITIAL_EXTENSIONS", _DEFAULT_EXTENSIONS)
    _clock(monkeypatch, _at(10, 30))
    _show(qtbot, window, Timeframe.MIN5)
    pm = _pm(window)

    def behind() -> bool:
        line = pm._last_price_line
        assert line is not None and pm._plots
        return all(line.zValue() < item.zValue() for item in pm._plots.values())  # type: ignore[attr-defined]

    assert behind()
    _poll(qtbot, window, _at(10, 41), monkeypatch)
    assert behind()


# ----------------------------------------------------------------------
# AC2, AC3, AC4, AC11: it follows every redraw
# ----------------------------------------------------------------------


def test_a_live_poll_moves_the_line_and_tag(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)

    bars = _poll(qtbot, window, _at(10, 41), monkeypatch)

    assert bars[-1].close == _forming_close(_at(10, 41))
    _assert_marker_at(window, bars)
    line = _line(window)
    assert line is not None
    assert line[0][-1] == pytest.approx(_view(window).viewRect().right())


def test_extension_renders_do_not_prune_the_marker(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window = _window(qtbot, tmp_path / "test.db", monkeypatch, source)
    monkeypatch.setattr(controller_module, "INITIAL_EXTENSIONS", _DEFAULT_EXTENSIONS)
    _clock(monkeypatch, _at(10, 30))
    shown = _show(qtbot, window, Timeframe.MIN5)
    assert [state.name for state in window._state.extensions][:3] == ["sma", "sma", "sma"]

    _assert_marker_at(window, shown)
    window._reload_extensions()
    _assert_marker_at(window, shown)


def test_a_timeframe_switch_leaves_one_tag_at_the_new_close(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    old_tag = _tag(window)

    window._state.timeframe = Timeframe.MIN15
    window._load()
    bars = _settled(qtbot, window)

    assert old_tag.scene() is None
    _assert_marker_at(window, bars)
    text = f"{bars[-1].close:.2f}"
    tags = [
        item
        for item in _view(window).scene().items()
        if isinstance(item, pg.TextItem) and item.textItem.toPlainText() == text
    ]
    assert tags == [_tag(window)]


def test_older_history_moves_the_line_to_the_newest_bars_new_index(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    deep_start = date(2024, 11, 4)
    db_path = tmp_path / "test.db"
    _seed(
        db_path,
        Timeframe.MIN30,
        _bars(Timeframe.MIN30, deep_start, _WEDNESDAY),
        _at(9, 30, deep_start),
        _WEDNESDAY_CLOSE,
    )
    window = _window(qtbot, db_path, monkeypatch, source)
    _clock(monkeypatch, _at(10, 30))
    shown = _show(qtbot, window, Timeframe.MIN30)

    window._on_older_history_wanted(2000)
    bars = _settled(qtbot, window)

    assert len(bars) > len(shown)
    _assert_marker_at(window, bars)


# ----------------------------------------------------------------------
# AC5, AC5b, AC6, AC7: the setting
# ----------------------------------------------------------------------


def test_turning_the_marker_off_and_on_applies_without_a_reload(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    old_tag = _tag(window)

    _pm(window).set_last_price_visible(False)
    _assert_no_marker(window)
    assert old_tag.scene() is None

    _pm(window).set_last_price_visible(True)
    _assert_marker_at(window, shown)


def test_off_survives_polls_reloads_and_loads(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    _pm(window).set_last_price_visible(False)

    _poll(qtbot, window, _at(10, 41), monkeypatch)
    _assert_no_marker(window)
    window._reload_extensions()
    _assert_no_marker(window)
    window._load()
    _settled(qtbot, window)
    _assert_no_marker(window)


def test_the_setting_defaults_on_and_persists(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.db")
    with Cache(db_path, registry=startup_registry()) as cache:
        assert cache.get_show_last_price_line() is True
        cache.set_show_last_price_line(False)
    with Cache(db_path, registry=startup_registry()) as cache:
        assert cache.get_show_last_price_line() is False
        cache.set_show_last_price_line(True)
        assert cache.get_show_last_price_line() is True


def test_a_new_window_starts_with_the_stored_setting(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        cache.set_show_last_price_line(False)

    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)

    _assert_no_marker(window)


@pytest.mark.parametrize("accepted", [True, False])
def test_the_settings_checkbox_saves_and_applies_only_on_ok(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
    accepted: bool,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    seen: list[bool] = []

    def run(dialog: ApplicationSettingsDialog) -> int:
        checkbox = dialog.findChild(QCheckBox, "showLastPriceLine")
        assert checkbox is not None
        seen.append(checkbox.isChecked())
        checkbox.setChecked(False)
        if accepted:
            dialog.accept()
            return int(QDialog.DialogCode.Accepted)
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(ApplicationSettingsDialog, "exec", run)

    window._on_application_settings()
    bars = _settled(qtbot, window)

    assert seen == [True]
    assert window._cache.get_show_last_price_line() is (not accepted)
    if accepted:
        _assert_no_marker(window)
    else:
        _assert_marker_at(window, bars)
    assert bars[-1] == shown[-1]


# ----------------------------------------------------------------------
# AC8, AC9, AC10: view and empty charts
# ----------------------------------------------------------------------


def test_the_tag_hides_while_the_price_is_out_of_view(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    viewbox = _view(window)
    rect = viewbox.viewRect()
    low, high = min(rect.top(), rect.bottom()), max(rect.top(), rect.bottom())
    tag = _tag(window)
    assert tag.isVisible()

    viewbox.setYRange(high + 50.0, high + 60.0, padding=0)
    qtbot.waitUntil(lambda: not tag.isVisible(), timeout=2000)

    viewbox.setYRange(low, high, padding=0)
    qtbot.waitUntil(tag.isVisible, timeout=2000)


def test_the_line_reaches_the_axis_after_a_far_pan(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    viewbox = _view(window)

    viewbox.setXRange(len(shown) + 20_000, len(shown) + 20_200, padding=0)

    def reaches_axis() -> bool:
        line = _line(window)
        return line is not None and line[0][-1] == pytest.approx(viewbox.viewRect().right())

    qtbot.waitUntil(reaches_axis, timeout=2000)


def test_no_marker_after_a_load_without_bars(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    warnings: list[str] = []
    monkeypatch.setattr(
        controller_module, "show_warning", lambda _parent, title, _message: warnings.append(title),
    )
    source.empty = True

    window._state.symbol = "ZZZZ"
    window._load()
    _settled(qtbot, window)

    assert warnings == ["No Data"]
    _assert_no_marker(window)


def test_drawing_an_empty_series_removes_the_marker(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)

    _pm(window)._update_last_price(OHLCVSeries(_SYMBOL, Timeframe.MIN5, []))

    _assert_no_marker(window)


# ----------------------------------------------------------------------
# AC12: horizontal-line tool tags are unchanged (characterization)
# ----------------------------------------------------------------------


def test_horizontal_line_tags_stay_on_the_axis_and_hidden_stays_hidden(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _EmptyableSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    pm = _pm(window)
    viewbox = _view(window)
    price = shown[-1].close
    pm.update_axis_price_label(AxisPriceLabelRender("h", price, "1.00", "#ff9800", "#ffffff"))
    tag: Any = pm._axis_price_labels["h"].text_item
    assert isinstance(tag, pg.TextItem)

    def on_axis() -> bool:
        y = viewbox.mapViewToScene(pg.Point(0.0, price)).y()
        return bool(
            tag.pos().x() == pytest.approx(viewbox.sceneBoundingRect().right())
            and tag.pos().y() == pytest.approx(y)
        )

    assert on_axis() and tag.isVisible()
    viewbox.setXRange(10.0, 210.0, padding=0)
    rect = viewbox.viewRect()
    viewbox.setYRange(rect.top() - 5.0, rect.bottom() - 5.0, padding=0)
    qtbot.waitUntil(on_axis, timeout=2000)
    assert tag.isVisible()

    pm.set_visible("h", False)
    viewbox.setXRange(20.0, 220.0, padding=0)
    viewbox.setYRange(price + 50.0, price + 60.0, padding=0)
    qtbot.waitUntil(on_axis, timeout=2000)
    assert not tag.isVisible()
    viewbox.setYRange(price - 5.0, price + 5.0, padding=0)
    qtbot.waitUntil(on_axis, timeout=2000)
    assert not tag.isVisible()
