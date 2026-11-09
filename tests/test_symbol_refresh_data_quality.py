"""
Data Quality state across a symbol refresh (criterion 6a): result rows,
the editor selection and comparisons from before the refresh can never be
edited or applied after it.
"""

import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import QDate, Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QDateEdit,
    QDialog,
    QGroupBox,
    QLabel,
    QLayoutItem,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QWidget,
)

import app.data_quality as data_quality_module
from app.bar_comparison import (
    BarComparisonResult,
    BarComparisonRow,
    BarComparisonRowKind,
)
from app.data_quality import DataQualityTab
from data.cache import Cache
from data.models import Bar, Timeframe


_NAMESPACE = "yfinance"
_SYMBOL = "AAPL"
_OTHER_SYMBOL = "MSFT"
_SESSION = datetime(2026, 2, 2, 5, 0, tzinfo=UTC)
_BOUNDARY = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)


def _never_cancel() -> bool:
    return False


def _bar(open_price: float, volume: int = 1_000) -> Bar:
    return Bar(
        _SESSION,
        open_price,
        open_price + 2.0,
        open_price - 2.0,
        open_price + 1.0,
        volume,
    )


_OLD_BAR = _bar(100.0)
_REFRESHED_BAR = _bar(200.0, volume=2_000)


class _ComparisonService:
    def __init__(self, blocking: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        if not blocking:
            self.release.set()

    def compare(
        self,
        origin_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        cached_bar: Bar,
        should_cancel: Callable[[], bool] = _never_cancel,
    ) -> BarComparisonResult:
        self.started.set()
        assert self.release.wait(10)
        return BarComparisonResult(
            rows=(
                BarComparisonRow(
                    origin_namespace,
                    "Cached Yahoo Finance",
                    BarComparisonRowKind.CACHED_ORIGIN,
                    cached_bar,
                ),
                BarComparisonRow(
                    origin_namespace,
                    "Refreshed Yahoo Finance",
                    BarComparisonRowKind.REFRESHED_ORIGIN,
                    _bar(150.0),
                ),
            ),
            suggestions=(),
            origin_unchanged=False,
        )


class _Refresher:
    """
    Stands in for MainWindow: confirming the refresh rewinds the symbol and
    refills it from the source, now or after a drain, and announces the
    rewind to every Data Quality tab.
    """

    def __init__(self, cache: Cache, defer_rewind: bool = False) -> None:
        self._cache = cache
        self._defer_rewind = defer_rewind
        self.plans: list[tuple[str, str]] = []
        self._rewound_slots: list[Callable[[str, str], None]] = []

    def plan_symbol_refresh(self, cache_namespace: str, symbol: str) -> Any:
        self.plans.append((cache_namespace, symbol))
        return data_quality_module.SymbolRefreshPlan(
            intraday_days=55,
            daily_since=date(2016, 1, 1),
            refresh=self._refresh,
        )

    def connect_symbol_rewound(self, slot: Callable[[str, str], None]) -> None:
        self._rewound_slots.append(slot)

    def symbol_refresh_status(self, cache_namespace: str, symbol: str) -> Any:
        return None

    def connect_symbol_refresh_status(
        self,
        slot: Callable[[str, str], None],
    ) -> None:
        pass

    def _refresh(self) -> None:
        if not self._defer_rewind:
            self.finish_drain()

    def finish_drain(self) -> None:
        self._cache.rewind_symbol_history(
            _NAMESPACE,
            _SYMBOL,
            {Timeframe.DAILY: int(_BOUNDARY.timestamp() * 1000)},
            (),
        )
        self._cache.put_bars(_NAMESPACE, _SYMBOL, Timeframe.DAILY, [_REFRESHED_BAR])
        for slot in self._rewound_slots:
            slot(_NAMESPACE, _SYMBOL)


class _Unavailable(_Refresher):
    def plan_symbol_refresh(self, cache_namespace: str, symbol: str) -> Any:
        return None


def _tab(
    qtbot: Any,
    cache: Cache,
    symbol_refresh: _Refresher,
    service: _ComparisonService | None = None,
) -> DataQualityTab:
    cache.put_bars(_NAMESPACE, _SYMBOL, Timeframe.DAILY, [_OLD_BAR])
    cache.put_bars(_NAMESPACE, _OTHER_SYMBOL, Timeframe.DAILY, [_OLD_BAR])
    tab = DataQualityTab(
        cache,
        comparison_service=service,
        initial_cache_namespace=_NAMESPACE,
        initial_symbol=_SYMBOL,
        initial_timeframe=Timeframe.DAILY,
        symbol_refresh=symbol_refresh,
    )
    qtbot.addWidget(tab)
    date_edit = tab.findChild(QDateEdit, "dataQualityDate")
    assert date_edit is not None
    date_edit.setDate(QDate(2026, 2, 2))
    return tab


def _load_and_select(tab: DataQualityTab, symbol: str = _SYMBOL) -> None:
    symbol_edit = tab.findChild(QLineEdit, "dataQualitySymbol")
    assert symbol_edit is not None
    symbol_edit.setText(symbol)
    _button(tab, "loadBarsForDate").click()
    table = _table(tab)
    assert table.rowCount() == 1
    table.setCurrentCell(0, 0)


def _confirm_refresh(
    tab: DataQualityTab,
    monkeypatch: pytest.MonkeyPatch,
    symbol: str = _SYMBOL,
) -> None:
    monkeypatch.setattr(
        data_quality_module.SymbolRefreshDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    symbol_edit = tab.findChild(QLineEdit, "dataQualitySymbol")
    assert symbol_edit is not None
    symbol_edit.setText(symbol)
    _button(tab, "refreshSymbolFromSource").click()


def _button(tab: DataQualityTab, name: str) -> QPushButton:
    button = tab.findChild(QPushButton, name)
    assert button is not None
    return button


def _table(tab: DataQualityTab) -> QTableWidget:
    table = tab.findChild(QTableWidget, "dataQualityBars")
    assert table is not None
    return table


def test_refresh_clears_rows_and_selection_so_a_volume_edit_adds_no_price_override(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        tab = _tab(qtbot, cache, _Refresher(cache))
        _load_and_select(tab)
        assert _button(tab, "applyBarCorrection").isEnabled()

        _confirm_refresh(tab, monkeypatch)

        # The pre-refresh row cannot be reselected or edited.
        assert _table(tab).rowCount() == 0
        assert not _button(tab, "applyBarCorrection").isEnabled()
        _load_and_select(tab)
        volume = tab.findChild(QLineEdit, "dataQualityVolume")
        assert volume is not None
        volume.setText("3,000")
        _button(tab, "applyBarCorrection").click()

        inspection = cache.get_bar_inspection(
            _NAMESPACE, _SYMBOL, Timeframe.DAILY, _SESSION,
        )

    assert inspection is not None
    assert inspection.raw_bar == _REFRESHED_BAR
    correction = inspection.correction
    assert correction is not None
    assert (
        correction.open,
        correction.high,
        correction.low,
        correction.close,
        correction.volume,
    ) == (None, None, None, None, 3_000)
    assert inspection.effective_bar.open == _REFRESHED_BAR.open


def test_refresh_discards_cached_comparisons_for_the_symbol_only(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        tab = _tab(qtbot, cache, _Refresher(cache), _ComparisonService())
        for symbol in (_OTHER_SYMBOL, _SYMBOL):
            _load_and_select(tab, symbol)
            _button(tab, "compareBarSources").click()
            qtbot.waitUntil(lambda: not tab.comparison_running(), timeout=5_000)
            assert _button(tab, "refreshProviderBar").isEnabled()

        _confirm_refresh(tab, monkeypatch)

        assert {key.symbol for key in tab._comparison_cache} == {_OTHER_SYMBOL}
        _load_and_select(tab)
        assert _button(tab, "compareBarSources").text() == "Compare sources"
        assert not _button(tab, "refreshProviderBar").isEnabled()
        assert tab._displayed_comparison_result is None


def test_comparison_runs_until_its_thread_finished_slot_has_run(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    service = _ComparisonService(blocking=True)
    with Cache(str(tmp_path / "test.db")) as cache:
        tab = _tab(qtbot, cache, _Refresher(cache), service)
        _load_and_select(tab, _OTHER_SYMBOL)
        _button(tab, "compareBarSources").click()
        first = tab._comparison_thread
        assert first is not None and service.started.wait(5)
        # The first thread stops before the main thread processes its queued
        # completed and finished deliveries.
        first.quit()
        service.release.set()
        assert first.wait(5_000)

        assert tab.comparison_running()
        _load_and_select(tab, _SYMBOL)
        _button(tab, "compareBarSources").click()
        assert tab._comparison_thread is first

        qtbot.waitUntil(lambda: not tab.comparison_running(), timeout=5_000)
        _button(tab, "compareBarSources").click()
        qtbot.waitUntil(lambda: not tab.comparison_running(), timeout=5_000)

        assert {key.symbol for key in tab._comparison_cache} == {
            _OTHER_SYMBOL,
            _SYMBOL,
        }
        assert _button(tab, "refreshProviderBar").isEnabled()


@pytest.mark.parametrize("rewind", ["immediate", "after-drain"])
def test_late_comparison_result_for_a_refreshed_symbol_is_rejected(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rewind: str,
) -> None:
    service = _ComparisonService(blocking=True)
    with Cache(str(tmp_path / "test.db")) as cache:
        refresher = _Refresher(cache, defer_rewind=rewind == "after-drain")
        tab = _tab(qtbot, cache, refresher, service)
        _load_and_select(tab)
        _button(tab, "compareBarSources").click()
        assert service.started.wait(5)

        _confirm_refresh(tab, monkeypatch)
        if rewind == "after-drain":
            # Until the rewind, the cache still holds the data on screen.
            assert _table(tab).rowCount() == 1
            refresher.finish_drain()
        service.release.set()
        qtbot.waitUntil(lambda: not tab.comparison_running(), timeout=5_000)
        qtbot.wait(50)

        assert tab._comparison_cache == {}
        assert tab._displayed_comparison_result is None
        assert _table(tab).rowCount() == 0
        assert not _button(tab, "refreshProviderBar").isEnabled()
        assert cache.get_bars(
            _NAMESPACE, _SYMBOL, Timeframe.DAILY, 0, 2**62,
        ) == [_REFRESHED_BAR]


def test_rows_of_another_symbol_survive_a_refresh(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        refresher = _Refresher(cache)
        tab = _tab(qtbot, cache, refresher)
        _load_and_select(tab, _OTHER_SYMBOL)

        _confirm_refresh(tab, monkeypatch, _SYMBOL)

        assert refresher.plans == [(_NAMESPACE, _SYMBOL)]
        assert _table(tab).rowCount() == 1
        assert _button(tab, "applyBarCorrection").isEnabled()


def test_unavailable_source_explains_and_asks_nothing(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        data_quality_module,
        "show_information",
        lambda _parent, title, message: messages.append((title, message)),
    )
    monkeypatch.setattr(
        data_quality_module.SymbolRefreshDialog,
        "exec",
        lambda _dialog: pytest.fail("no confirmation expected"),
    )
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("alpaca:iex", _SYMBOL, Timeframe.DAILY, [_OLD_BAR])
        tab = _tab(qtbot, cache, _Unavailable(cache))
        source = tab.findChild(QComboBox, "dataQualitySource")
        assert source is not None
        source.setCurrentIndex(source.findData("alpaca:iex"))

        _button(tab, "refreshSymbolFromSource").click()

    assert messages == [
        (
            "Refresh Unavailable",
            "Alpaca / IEX is not connected in this session. Make it the "
            "active data source, then refresh AAPL.",
        )
    ]


def test_refresh_button_is_disabled_without_a_refresh_handler(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        tab = DataQualityTab(cache)
        qtbot.addWidget(tab)

        assert not _button(tab, "refreshSymbolFromSource").isEnabled()


@pytest.mark.parametrize(
    ("namespace", "days", "since", "source"),
    [
        pytest.param("yfinance", 54, date(2016, 1, 1), "Yahoo Finance", id="yahoo"),
        pytest.param("alpaca:iex", 600, date(2016, 1, 1), "Alpaca / IEX", id="alpaca"),
        pytest.param(
            "alpaca:sip", 12, date(2019, 3, 4), "Alpaca / SIP (real-time)", id="sip",
        ),
    ],
)
def test_confirmation_asks_yes_or_no_with_the_plan_ranges(
    qtbot: Any,
    namespace: str,
    days: int,
    since: date,
    source: str,
) -> None:
    plan = data_quality_module.SymbolRefreshPlan(
        intraday_days=days,
        daily_since=since,
        refresh=lambda: None,
    )

    dialog = data_quality_module.SymbolRefreshDialog(namespace, _SYMBOL, plan)
    qtbot.addWidget(dialog)

    message = dialog.findChild(QLabel, "symbolRefreshMessage")
    assert message is not None
    assert dialog.windowTitle() == "Refresh AAPL?"
    assert message.text() == (
        f"{days} days of intraday data and daily history since {since.year} "
        f"can safely be refreshed from {source}. Proceed?"
    )
    buttons = dialog.findChildren(QPushButton)
    assert [button.text() for button in buttons] == ["Yes", "No"]
    assert [button.isDefault() for button in buttons] == [False, True]


@pytest.mark.parametrize(
    ("button", "result"),
    [
        pytest.param("confirmSymbolRefresh", QDialog.DialogCode.Accepted, id="yes"),
        pytest.param("cancelSymbolRefresh", QDialog.DialogCode.Rejected, id="no"),
        pytest.param(None, QDialog.DialogCode.Rejected, id="enter"),
    ],
)
def test_confirmation_yes_accepts_and_no_or_enter_rejects(
    qtbot: Any,
    button: str | None,
    result: QDialog.DialogCode,
) -> None:
    plan = data_quality_module.SymbolRefreshPlan(
        intraday_days=54,
        daily_since=date(2016, 1, 1),
        refresh=lambda: None,
    )
    dialog = data_quality_module.SymbolRefreshDialog("yfinance", _SYMBOL, plan)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    if button is None:
        qtbot.keyClick(dialog, Qt.Key.Key_Return)
    else:
        clicked = dialog.findChild(QPushButton, button)
        assert clicked is not None
        clicked.click()

    assert dialog.result() == result


# ----------------------------------------------------------------------
# 6e: a tab opened after the confirmation, before the rewind
# ----------------------------------------------------------------------


def test_tab_opened_before_the_rewind_drops_rows_and_comparisons(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ComparisonService()
    with Cache(str(tmp_path / "test.db")) as cache:
        refresher = _Refresher(cache, defer_rewind=True)
        confirming = _tab(qtbot, cache, refresher)
        _confirm_refresh(confirming, monkeypatch)
        # Settings is closed and opened again while the drain runs.
        confirming.shutdown_comparison()
        reopened = _tab(qtbot, cache, refresher, service)
        _load_and_select(reopened, _OTHER_SYMBOL)
        _button(reopened, "compareBarSources").click()
        qtbot.waitUntil(lambda: not reopened.comparison_running(), timeout=5_000)
        service.release.clear()
        service.started.clear()
        _load_and_select(reopened)
        _button(reopened, "compareBarSources").click()
        assert service.started.wait(5)

        refresher.finish_drain()

        assert _table(reopened).rowCount() == 0
        assert not _button(reopened, "applyBarCorrection").isEnabled()
        service.release.set()
        qtbot.waitUntil(lambda: not reopened.comparison_running(), timeout=5_000)
        qtbot.wait(50)
        assert {key.symbol for key in reopened._comparison_cache} == {_OTHER_SYMBOL}
        assert reopened._displayed_comparison_result is None
        assert _table(reopened).rowCount() == 0

        _load_and_select(reopened)
        volume = reopened.findChild(QLineEdit, "dataQualityVolume")
        assert volume is not None
        volume.setText("3,000")
        _button(reopened, "applyBarCorrection").click()
        inspection = cache.get_bar_inspection(
            _NAMESPACE, _SYMBOL, Timeframe.DAILY, _SESSION,
        )

    assert inspection is not None
    assert inspection.raw_bar == _REFRESHED_BAR
    correction = inspection.correction
    assert correction is not None
    assert (
        correction.open,
        correction.high,
        correction.low,
        correction.close,
        correction.volume,
    ) == (None, None, None, None, 3_000)


# ----------------------------------------------------------------------
# G2 layout: scope and refresh on top, inspection in its own group box
# ----------------------------------------------------------------------


def _layout_item(layout: QBoxLayout, index: int) -> QLayoutItem:
    item = layout.itemAt(index)
    assert item is not None
    return item


def test_top_row_holds_scope_and_refresh_and_a_group_box_holds_inspection(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        tab = _tab(qtbot, cache, _Refresher(cache))

        layout = tab.layout()
        assert isinstance(layout, QBoxLayout)
        top_row = _layout_item(layout, 0).layout()
        assert isinstance(top_row, QBoxLayout)
        top_widgets = [
            widget.objectName()
            for widget in (
                _layout_item(top_row, i).widget() for i in range(top_row.count())
            )
            if widget is not None and widget.objectName()
        ]
        status = _layout_item(layout, 1).widget()
        group = _layout_item(layout, 2).widget()

    assert top_widgets == [
        "dataQualitySource",
        "dataQualitySymbol",
        "refreshSymbolFromSource",
    ]
    assert isinstance(status, QLabel)
    assert status.objectName() == "symbolRefreshStatus"
    assert isinstance(group, QGroupBox)
    assert group.title() == "Inspect and correct bars"
    for name in (
        "dataQualityTimeframe",
        "dataQualityDeviation",
        "findSuspiciousBars",
        "dataQualityDate",
        "loadBarsForDate",
        "dataQualityBars",
        "applyBarCorrection",
        "compareBarSources",
        "dataQualityComparisonTable",
    ):
        widget = tab.findChild(QWidget, name)
        assert widget is not None and group.isAncestorOf(widget), name
    titles = {box.title() for box in group.findChildren(QGroupBox)}
    assert titles == {"Selected bar", "Compare sources"}


# ----------------------------------------------------------------------
# The refresh status banner
# ----------------------------------------------------------------------


class _StatusRefresher(_Refresher):
    """A refresher whose status for every symbol is set by the test."""

    def __init__(self, cache: Cache) -> None:
        super().__init__(cache)
        self.status: data_quality_module.SymbolRefreshStatus | None = None
        self._status_slots: list[Callable[[str, str], None]] = []

    def symbol_refresh_status(self, cache_namespace: str, symbol: str) -> Any:
        return self.status

    def connect_symbol_refresh_status(
        self,
        slot: Callable[[str, str], None],
    ) -> None:
        self._status_slots.append(slot)

    def set_status(
        self,
        status: data_quality_module.SymbolRefreshStatus | None,
    ) -> None:
        self.status = status
        for slot in self._status_slots:
            slot(_NAMESPACE, _SYMBOL)


def _banner(tab: DataQualityTab) -> QLabel:
    label = tab.findChild(QLabel, "symbolRefreshStatus")
    assert label is not None
    label.ensurePolished()
    return label


def _text_color(label: QLabel) -> str:
    return label.palette().color(QPalette.ColorRole.WindowText).name()


_WAITING_TEXT = "Waiting for the current chart load to finish…"
_REFRESHING_TEXT = f"Refreshing {_SYMBOL}…"
_ON_SCREEN_TEXT = (
    f"{_SYMBOL} refreshed at 9:30 AM. Other timeframes update when you view them."
)
_OFF_SCREEN_TEXT = f"{_SYMBOL} refreshed at 9:30 AM. It downloads when you open it."
_FAILED_TEXT = (
    f"{_SYMBOL} refresh stopped: the download failed. "
    "The missing data downloads on the next load."
)


def test_empty_status_hides_the_banner(qtbot: Any, tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        tab = _tab(qtbot, cache, _StatusRefresher(cache))

        assert _banner(tab).isHidden()


@pytest.mark.parametrize(
    ("state", "text"),
    [
        ("running", _WAITING_TEXT),
        ("running", _REFRESHING_TEXT),
        ("done", _ON_SCREEN_TEXT),
        ("done", _OFF_SCREEN_TEXT),
        ("failed", _FAILED_TEXT),
    ],
    ids=["waiting", "refreshing", "done-on-screen", "done-off-screen", "failed"],
)
def test_status_banner_shows_the_state_of_the_refresh(
    qtbot: Any,
    tmp_path: Path,
    state: data_quality_module.SymbolRefreshState,
    text: str,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        refresher = _StatusRefresher(cache)
        refresher.status = data_quality_module.SymbolRefreshStatus(state, text)
        tab = _tab(qtbot, cache, refresher)

        banner = _banner(tab)

        assert not banner.isHidden()
        assert banner.property("refreshState") == state
        assert banner.text() == text


def test_banner_recolors_and_hides_as_the_status_changes(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        refresher = _StatusRefresher(cache)
        tab = _tab(qtbot, cache, refresher)
        seen: list[tuple[bool, str]] = []
        for status in (
            data_quality_module.SymbolRefreshStatus("running", _REFRESHING_TEXT),
            data_quality_module.SymbolRefreshStatus("done", _ON_SCREEN_TEXT),
            data_quality_module.SymbolRefreshStatus("failed", _FAILED_TEXT),
            None,
        ):
            refresher.set_status(status)
            banner = _banner(tab)
            seen.append((banner.isHidden(), _text_color(banner)))

    assert seen[:3] == [
        (False, "#33414d"),
        (False, "#0b5cad"),
        (False, "#a12e2e"),
    ]
    assert seen[3][0]
