from datetime import UTC, datetime
from pathlib import Path
import threading
from typing import Any

import pytest
from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
)

import app.data_quality as data_quality_module
from app.application_settings import ApplicationSettingsDialog
from app.bar_comparison import (
    BarComparisonResult,
    BarComparisonRow,
    BarComparisonRowKind,
    BarFieldSuggestion,
)
from app.data_quality import DataQualityTab
from data.cache import Cache
from data.models import Bar, BarCorrection, Timeframe
from data.provider import ProviderAvailability
from data.provider.config import (
    ALPACA_PAPER_CONNECTION_ID,
    MarketDataFeed,
)
from data.provider.credentials import ProviderCredentials


_AVAILABLE_PROVIDERS = {
    "yfinance": ProviderAvailability(True, None),
    "alpaca": ProviderAvailability(True, None),
}
_SESSION = datetime(2026, 2, 2, 5, 0, tzinfo=UTC)


class _MemoryCredentialStore:
    def get(self, connection_id: str) -> ProviderCredentials | None:
        return None

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None:
        pass

    def delete(self, connection_id: str) -> None:
        pass


class _ComparisonService:
    def __init__(
        self,
        results: dict[datetime, BarComparisonResult],
    ) -> None:
        self.results = results
        self.calls: list[tuple[str, str, Timeframe, Bar]] = []
        self.thread_ids: list[int] = []

    def compare(
        self,
        origin_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        cached_bar: Bar,
    ) -> BarComparisonResult:
        self.calls.append((origin_namespace, symbol, timeframe, cached_bar))
        self.thread_ids.append(threading.get_ident())
        return self.results[cached_bar.timestamp]


class _BlockingComparisonService(_ComparisonService):
    def __init__(self, result: BarComparisonResult) -> None:
        super().__init__({_SESSION: result})
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def compare(
        self,
        origin_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        cached_bar: Bar,
    ) -> BarComparisonResult:
        self.started.set()
        self.release.wait(timeout=2.0)
        result = super().compare(
            origin_namespace,
            symbol,
            timeframe,
            cached_bar,
        )
        self.finished.set()
        return result


def test_settings_data_quality_tab_uses_current_chart_context(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.set_provider_connection_feed(
            ALPACA_PAPER_CONNECTION_ID,
            MarketDataFeed.DELAYED_SIP,
        )
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bad_spy_bar()],
        )
        dialog = ApplicationSettingsDialog(
            cache,
            _MemoryCredentialStore(),
            _AVAILABLE_PROVIDERS,
            active_connection_id=ALPACA_PAPER_CONNECTION_ID,
            current_symbol="SPY",
            current_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(dialog)

        tab = dialog.findChild(DataQualityTab, "dataQualityTab")
        source = dialog.findChild(QComboBox, "dataQualitySource")
        symbol = dialog.findChild(QLineEdit, "dataQualitySymbol")
        timeframe = dialog.findChild(QComboBox, "dataQualityTimeframe")

        assert tab is not None
        assert source is not None
        assert source.currentData() == "alpaca:delayed_sip"
        assert symbol is not None
        assert symbol.text() == "SPY"
        assert timeframe is not None
        assert timeframe.currentData() == Timeframe.DAILY


def test_find_suspicious_bar_populates_editor(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bad_spy_bar()],
        )
        tab = DataQualityTab(
            cache,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)

        find_button = tab.findChild(QPushButton, "findSuspiciousBars")
        table = tab.findChild(QTableWidget, "dataQualityBars")
        low = tab.findChild(QDoubleSpinBox, "dataQualityLow")
        apply_button = tab.findChild(QPushButton, "applyBarCorrection")

        assert find_button is not None
        assert table is not None
        assert low is not None
        assert apply_button is not None
        find_button.click()
        assert table.rowCount() == 1
        table.setCurrentCell(0, 0)

        assert low.value() == pytest.approx(68.64)
        assert apply_button.isEnabled()
        assert _cell_text(table, 0, 6) == "79,286,521"
        assert _cell_text(table, 0, 8) == "Uncorrected"


def test_apply_and_restore_bar_correction(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bad_spy_bar()],
        )
        tab = DataQualityTab(
            cache,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        find_button = _button(tab, "findSuspiciousBars")
        apply_button = _button(tab, "applyBarCorrection")
        restore_button = _button(tab, "restoreProviderBar")
        table = _table(tab)
        low = tab.findChild(QDoubleSpinBox, "dataQualityLow")
        assert low is not None

        find_button.click()
        table.setCurrentCell(0, 0)
        low.setValue(685.77)

        with qtbot.waitSignal(tab.bars_changed):
            apply_button.click()

        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SESSION,
        )
        assert inspection is not None
        assert inspection.correction is not None
        assert inspection.correction.low == pytest.approx(685.77)
        assert _cell_text(table, 0, 8) == "Corrected"
        assert restore_button.isEnabled()

        with qtbot.waitSignal(tab.bars_changed):
            restore_button.click()

        restored = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SESSION,
        )
        assert restored is not None
        assert restored.correction is None
        assert restored.effective_bar.low == pytest.approx(68.64)
        assert _cell_text(table, 0, 8) == "Uncorrected"


def test_apply_preserves_undisplayed_provider_precision(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    precise = Bar(
        timestamp=_SESSION,
        open=685.912345678901,
        high=693.212345678901,
        low=68.64,
        close=691.712345678901,
        volume=79_286_521,
    )
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [precise],
        )
        tab = DataQualityTab(
            cache,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        _button(tab, "findSuspiciousBars").click()
        _table(tab).setCurrentCell(0, 0)
        low = tab.findChild(QDoubleSpinBox, "dataQualityLow")
        assert low is not None
        low.setValue(685.77)

        _button(tab, "applyBarCorrection").click()

        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SESSION,
        )
        assert inspection is not None
        assert inspection.correction is not None
        assert inspection.correction.low == pytest.approx(685.77)
        assert inspection.correction.open is None
        assert inspection.correction.high is None
        assert inspection.correction.close is None
        assert inspection.correction.volume is None


def test_volume_editor_accepts_displayed_group_separators(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bad_spy_bar()],
        )
        tab = DataQualityTab(
            cache,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        _button(tab, "findSuspiciousBars").click()
        _table(tab).setCurrentCell(0, 0)
        low = tab.findChild(QDoubleSpinBox, "dataQualityLow")
        volume = tab.findChild(QLineEdit, "dataQualityVolume")
        assert low is not None
        assert volume is not None
        low.setValue(685.77)
        volume.setText("79,286,521")

        _button(tab, "applyBarCorrection").click()

        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SESSION,
        )
        assert inspection is not None
        assert inspection.correction is not None
        assert inspection.correction.low == pytest.approx(685.77)
        assert inspection.correction.volume is None


def test_load_date_finds_after_hours_bar_by_new_york_date(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    after_hours = datetime(2026, 2, 3, 0, 45, tzinfo=UTC)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.MIN15,
            [
                Bar(after_hours, 100.0, 101.0, 99.0, 100.0, 1_000),
            ],
        )
        tab = DataQualityTab(
            cache,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.MIN15,
        )
        qtbot.addWidget(tab)
        requested_date = tab.findChild(QDateEdit, "dataQualityDate")
        load_button = _button(tab, "loadBarsForDate")
        table = _table(tab)
        assert requested_date is not None
        requested_date.setDate(QDate(2026, 2, 2))

        load_button.click()

        assert table.rowCount() == 1
        assert _cell_text(table, 0, 0) == "2026-02-02"
        assert _cell_text(table, 0, 1) == "19:45"


def test_daily_yahoo_bar_displays_its_utc_session_date(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    yahoo_session = datetime(2026, 2, 2, 0, 0, tzinfo=UTC)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            [Bar(yahoo_session, 100.0, 101.0, 99.0, 100.0, 1_000)],
        )
        tab = DataQualityTab(
            cache,
            initial_cache_namespace="yfinance",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        requested_date = tab.findChild(QDateEdit, "dataQualityDate")
        assert requested_date is not None
        requested_date.setDate(QDate(2026, 2, 2))

        _button(tab, "loadBarsForDate").click()

        assert _cell_text(_table(tab), 0, 0) == "2026-02-02"


def test_settings_tracks_immediately_applied_bar_changes(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            [_bad_spy_bar()],
        )
        dialog = ApplicationSettingsDialog(
            cache,
            _MemoryCredentialStore(),
            _AVAILABLE_PROVIDERS,
            current_symbol="SPY",
            current_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(dialog)
        tab = dialog.findChild(DataQualityTab, "dataQualityTab")
        assert tab is not None
        _button(tab, "findSuspiciousBars").click()
        _table(tab).setCurrentCell(0, 0)
        low = tab.findChild(QDoubleSpinBox, "dataQualityLow")
        assert low is not None
        low.setValue(685.77)

        _button(tab, "applyBarCorrection").click()

        assert dialog.bars_changed()


def test_invalid_editor_values_show_validation_message(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[tuple[str, str]] = []

    def capture_warning(
        _parent: object,
        title: str,
        message: str,
    ) -> None:
        warnings.append((title, message))

    monkeypatch.setattr(data_quality_module, "show_warning", capture_warning)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bad_spy_bar()],
        )
        tab = DataQualityTab(
            cache,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        _button(tab, "findSuspiciousBars").click()
        _table(tab).setCurrentCell(0, 0)
        high = tab.findChild(QDoubleSpinBox, "dataQualityHigh")
        assert high is not None
        high.setValue(680.0)

        _button(tab, "applyBarCorrection").click()

        assert warnings == [
            (
                "Invalid Correction",
                "Corrected high cannot be below open or close.",
            )
        ]
        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SESSION,
        )
        assert inspection is not None
        assert inspection.correction is None

def test_correction_conflict_is_visible_and_editable(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    initial = Bar(_SESSION, 100.0, 110.0, 90.0, 105.0, 1_000)
    revised = Bar(_SESSION, 90.0, 110.0, 85.0, 90.0, 1_000)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("alpaca:delayed_sip", "SPY", Timeframe.DAILY, [initial])
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SESSION,
                low=95.0,
            )
        )
        cache.put_bars("alpaca:delayed_sip", "SPY", Timeframe.DAILY, [revised])
        tab = DataQualityTab(
            cache,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        requested_date = tab.findChild(QDateEdit, "dataQualityDate")
        assert requested_date is not None
        requested_date.setDate(QDate(2026, 2, 2))

        _button(tab, "loadBarsForDate").click()
        table = _table(tab)
        table.setCurrentCell(0, 0)
        status = tab.findChild(QLabel, "dataQualityCorrectionStatus")

        assert _cell_text(table, 0, 8) == "Correction conflict"
        assert status is not None
        assert "low" in status.text()
        assert _button(tab, "applyBarCorrection").isEnabled()
        assert _button(tab, "restoreProviderBar").isEnabled()


def test_compare_sources_runs_off_thread_and_shows_no_revision_message(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    cached = _bad_spy_bar()
    result = _comparison_result(cached, cached)
    service = _ComparisonService({_SESSION: result})
    main_thread_id = threading.get_ident()
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [cached],
        )
        tab = DataQualityTab(
            cache,
            comparison_service=service,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        _button(tab, "findSuspiciousBars").click()
        _table(tab).setCurrentCell(0, 0)

        _button(tab, "compareBarSources").click()
        comparison = _comparison_table(tab)
        qtbot.waitUntil(lambda: comparison.rowCount() == 2)
        message = tab.findChild(QLabel, "dataQualityComparisonMessage")

        assert message is not None
        assert "unchanged since it was cached" in message.text()
        assert _cell_text(comparison, 1, 6) == "Unchanged"
        assert service.thread_ids == [service.thread_ids[0]]
        assert service.thread_ids[0] != main_thread_id
        assert _button(tab, "compareBarSources").text() == "Compare again"


def test_comparison_cell_copies_only_selected_field_to_editor(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    cached = _bad_spy_bar()
    yahoo = Bar(_SESSION, 685.8, 693.0, 686.1, 691.6, 78_000_000)
    result = _comparison_result(
        cached,
        Bar(_SESSION, 685.9, 693.21, 685.77, 691.7, 79_286_521),
        yahoo=yahoo,
        low_corroborated=True,
    )
    service = _ComparisonService({_SESSION: result})
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [cached],
        )
        tab = DataQualityTab(
            cache,
            comparison_service=service,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        _button(tab, "findSuspiciousBars").click()
        _table(tab).setCurrentCell(0, 0)
        _button(tab, "compareBarSources").click()
        comparison = _comparison_table(tab)
        qtbot.waitUntil(lambda: comparison.rowCount() == 3)

        assert _cell_text(comparison, 1, 6) == "Provider revision — corroborated"
        assert _cell_text(comparison, 2, 6) == "Corroborates provider revision"

        comparison.setCurrentCell(2, 3)
        _button(tab, "useComparisonValue").click()
        low = tab.findChild(QDoubleSpinBox, "dataQualityLow")
        open_editor = tab.findChild(QDoubleSpinBox, "dataQualityOpen")
        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SESSION,
        )

        assert low is not None
        assert low.value() == pytest.approx(686.1)
        assert open_editor is not None
        assert open_editor.value() == pytest.approx(cached.open)
        assert inspection is not None
        assert inspection.correction is None

        comparison.setCurrentCell(2, 5)
        assert not _button(tab, "useComparisonValue").isEnabled()
        comparison.setCurrentCell(1, 5)
        assert _button(tab, "useComparisonValue").isEnabled()


def test_comparison_result_is_cached_until_compare_again(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    first = _bad_spy_bar()
    second_timestamp = datetime(2026, 2, 3, 5, 0, tzinfo=UTC)
    second = Bar(second_timestamp, 680.0, 690.0, 100.0, 685.0, 1_000)
    service = _ComparisonService(
        {
            _SESSION: _comparison_result(first, first),
            second_timestamp: _comparison_result(second, second),
        }
    )
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            [first, second],
        )
        tab = DataQualityTab(
            cache,
            comparison_service=service,
            initial_cache_namespace="yfinance",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        _button(tab, "findSuspiciousBars").click()
        table = _table(tab)
        table.setCurrentCell(0, 0)
        _button(tab, "compareBarSources").click()
        qtbot.waitUntil(lambda: len(service.calls) == 1)
        qtbot.waitUntil(lambda: _comparison_table(tab).rowCount() == 2)

        table.setCurrentCell(1, 0)
        table.setCurrentCell(0, 0)

        assert _comparison_table(tab).rowCount() == 2
        assert len(service.calls) == 1

        _button(tab, "compareBarSources").click()
        qtbot.waitUntil(lambda: len(service.calls) == 2)


def test_refresh_provider_bar_replaces_raw_bar_and_invalidates_comparison(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    cached = _bad_spy_bar()
    refreshed = Bar(
        _SESSION,
        cached.open,
        cached.high,
        685.77,
        cached.close,
        cached.volume,
        cached.vwap,
    )
    service = _ComparisonService(
        {_SESSION: _comparison_result(cached, refreshed)}
    )
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [cached],
        )
        cache.put_bar_correction(
            BarCorrection(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.DAILY,
                _SESSION,
                low=refreshed.low,
            )
        )
        tab = DataQualityTab(
            cache,
            comparison_service=service,
            initial_cache_namespace="alpaca:delayed_sip",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(tab)
        _button(tab, "findSuspiciousBars").click()
        table = _table(tab)
        table.setCurrentCell(0, 0)
        _button(tab, "compareBarSources").click()
        comparison = _comparison_table(tab)
        qtbot.waitUntil(lambda: comparison.rowCount() == 2)

        with qtbot.waitSignal(tab.bars_changed):
            _button(tab, "refreshProviderBar").click()

        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SESSION,
        )
        assert inspection is not None
        assert inspection.raw_bar.low == pytest.approx(685.77)
        assert inspection.correction is None
        assert _cell_text(table, 0, 4) == "685.77"
        assert _cell_text(table, 0, 7) == ""
        assert _cell_text(table, 0, 8) == "Uncorrected"
        assert comparison.rowCount() == 0
        assert _button(tab, "compareBarSources").text() == "Compare sources"
        assert not _button(tab, "refreshProviderBar").isEnabled()
        assert len(service.calls) == 1

        _button(tab, "compareBarSources").click()
        qtbot.waitUntil(lambda: len(service.calls) == 2)


def test_synthesized_bar_explains_comparison_is_unavailable(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    cached = _bad_spy_bar()
    service = _ComparisonService({_SESSION: _comparison_result(cached, cached)})
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("yfinance", "SPY", Timeframe.MIN39, [cached])
        tab = DataQualityTab(
            cache,
            comparison_service=service,
            initial_cache_namespace="yfinance",
            initial_symbol="SPY",
            initial_timeframe=Timeframe.MIN39,
        )
        qtbot.addWidget(tab)
        _button(tab, "findSuspiciousBars").click()
        _table(tab).setCurrentCell(0, 0)
        message = tab.findChild(QLabel, "dataQualityComparisonMessage")

        assert message is not None
        assert "synthesized by SimpleChart" in message.text()
        assert not _button(tab, "compareBarSources").isEnabled()
        assert service.calls == []


def test_settings_detaches_running_comparison_until_application_shutdown(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    cached = _bad_spy_bar()
    service = _BlockingComparisonService(_comparison_result(cached, cached))
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("yfinance", "SPY", Timeframe.DAILY, [cached])
        dialog = ApplicationSettingsDialog(
            cache,
            _MemoryCredentialStore(),
            _AVAILABLE_PROVIDERS,
            comparison_service=service,
            current_symbol="SPY",
            current_timeframe=Timeframe.DAILY,
        )
        qtbot.addWidget(dialog)
        tab = dialog.findChild(DataQualityTab, "dataQualityTab")
        assert tab is not None
        _button(tab, "findSuspiciousBars").click()
        _table(tab).setCurrentCell(0, 0)
        _button(tab, "compareBarSources").click()
        qtbot.waitUntil(service.started.is_set)

        dialog.reject()

        assert not service.finished.is_set()
        assert not tab.comparison_running()
        owner = data_quality_module._comparison_thread_owner()
        assert owner.parent() is QApplication.instance()
        assert owner.active_count() == 1

        release_timer = threading.Timer(0.01, service.release.set)
        release_timer.start()
        owner.shutdown()
        release_timer.join()

        assert service.finished.is_set()
        assert owner.active_count() == 0
        assert not tab.comparison_running()


def _cell_text(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None
    return item.text()


def _comparison_result(
    cached: Bar,
    refreshed: Bar,
    *,
    yahoo: Bar | None = None,
    low_corroborated: bool = False,
) -> BarComparisonResult:
    rows = [
        BarComparisonRow(
            "alpaca:delayed_sip",
            "Cached Alpaca / SIP (15-minute delayed)",
            BarComparisonRowKind.CACHED_ORIGIN,
            cached,
        ),
        BarComparisonRow(
            "alpaca:delayed_sip",
            "Refreshed Alpaca / SIP (15-minute delayed)",
            BarComparisonRowKind.REFRESHED_ORIGIN,
            refreshed,
        ),
    ]
    if yahoo is not None:
        rows.append(
            BarComparisonRow(
                "yfinance",
                "Yahoo Finance",
                BarComparisonRowKind.CORROBORATION,
                yahoo,
            )
        )
    suggestions = (
        (
            BarFieldSuggestion(
                "low",
                cached.low,
                refreshed.low,
                ("yfinance",) if low_corroborated else (),
            ),
        )
        if cached.low != refreshed.low
        else ()
    )
    return BarComparisonResult(
        tuple(rows),
        suggestions,
        origin_unchanged=not suggestions,
    )


def _bad_spy_bar() -> Bar:
    return Bar(
        timestamp=_SESSION,
        open=685.9,
        high=693.21,
        low=68.64,
        close=691.7,
        volume=79_286_521,
        vwap=691.25,
    )


def _button(parent: DataQualityTab, name: str) -> QPushButton:
    button = parent.findChild(QPushButton, name)
    assert button is not None
    return button


def _table(parent: DataQualityTab) -> QTableWidget:
    table = parent.findChild(QTableWidget, "dataQualityBars")
    assert table is not None
    return table


def _comparison_table(parent: DataQualityTab) -> QTableWidget:
    table = parent.findChild(QTableWidget, "dataQualityComparisonTable")
    assert table is not None
    return table
