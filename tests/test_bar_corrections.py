from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from data.cache import Cache
from data.models import Bar, BarCorrection, Timeframe


_SPY_SESSION = datetime(2026, 2, 2, 5, 0, tzinfo=UTC)


def test_bar_correction_overrides_only_selected_field(
    tmp_path: Path,
) -> None:
    raw = _bar(_SPY_SESSION, low=68.64)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("alpaca:delayed_sip", "SPY", Timeframe.DAILY, [raw])
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                low=685.77,
            )
        )

        effective = cache.get_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _timestamp_ms(_SPY_SESSION),
            _timestamp_ms(_SPY_SESSION),
        )
        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )

    assert effective == [_bar(_SPY_SESSION, low=685.77)]
    assert inspection is not None
    assert inspection.raw_bar == raw
    assert inspection.effective_bar == effective[0]
    correction = inspection.correction
    assert correction is not None
    assert correction.low == pytest.approx(685.77)
    assert correction.open is None
    assert correction.high is None
    assert correction.close is None
    assert correction.volume is None


def test_bar_correction_stores_only_values_that_differ_from_provider(
    tmp_path: Path,
) -> None:
    raw = _bar(_SPY_SESSION, low=68.64)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("alpaca:delayed_sip", "SPY", Timeframe.DAILY, [raw])
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                open=raw.open,
                high=raw.high,
                low=685.77,
                close=raw.close,
                volume=raw.volume,
            )
        )

        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )

    assert inspection is not None
    assert inspection.correction is not None
    assert inspection.correction.open is None
    assert inspection.correction.high is None
    assert inspection.correction.low == pytest.approx(685.77)
    assert inspection.correction.close is None
    assert inspection.correction.volume is None


def test_put_bar_correction_replaces_the_complete_override_set(
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION, low=68.64)],
        )
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                low=685.77,
            )
        )
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                high=700.0,
            )
        )

        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )

    assert inspection is not None
    assert inspection.correction is not None
    assert inspection.correction.high == pytest.approx(700.0)
    assert inspection.correction.low is None


def test_provider_refresh_preserves_only_the_corrected_field(
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION, low=68.64)],
        )
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                low=685.77,
            )
        )
        refreshed = Bar(
            timestamp=_SPY_SESSION,
            open=686.0,
            high=694.0,
            low=69.0,
            close=692.0,
            volume=80_000_000,
            vwap=691.5,
        )
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [refreshed],
        )

        effective = cache.get_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _timestamp_ms(_SPY_SESSION),
            _timestamp_ms(_SPY_SESSION),
        )

    assert effective == [
        Bar(
            timestamp=_SPY_SESSION,
            open=686.0,
            high=694.0,
            low=685.77,
            close=692.0,
            volume=80_000_000,
            vwap=691.5,
        )
    ]


def test_provider_refresh_conflict_falls_back_to_raw_provider_bar(
    tmp_path: Path,
) -> None:
    timestamp_ms = _timestamp_ms(_SPY_SESSION)
    initial = Bar(_SPY_SESSION, 100.0, 110.0, 90.0, 105.0, 1_000)
    revised = Bar(_SPY_SESSION, 90.0, 110.0, 85.0, 90.0, 1_000)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("alpaca:delayed_sip", "SPY", Timeframe.DAILY, [initial])
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                low=95.0,
            )
        )
        cache.put_bars("alpaca:delayed_sip", "SPY", Timeframe.DAILY, [revised])

        effective = cache.get_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            timestamp_ms,
            timestamp_ms,
        )
        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )
        conflict_count = cache.count_bar_correction_conflicts(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            timestamp_ms,
            timestamp_ms,
        )

    assert effective == [revised]
    assert inspection is not None
    assert inspection.correction_error is not None
    assert "low" in inspection.correction_error
    assert conflict_count == 1


def test_correction_is_scoped_to_provider_feed(
    tmp_path: Path,
) -> None:
    delayed = _bar(_SPY_SESSION, low=68.64)
    iex = _bar(_SPY_SESSION, low=685.77, volume=1_800_000)
    timestamp_ms = _timestamp_ms(_SPY_SESSION)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("alpaca:delayed_sip", "SPY", Timeframe.DAILY, [delayed])
        cache.put_bars("alpaca:iex", "SPY", Timeframe.DAILY, [iex])
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                low=685.77,
            )
        )

        delayed_result = cache.get_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            timestamp_ms,
            timestamp_ms,
        )
        iex_result = cache.get_bars(
            "alpaca:iex",
            "SPY",
            Timeframe.DAILY,
            timestamp_ms,
            timestamp_ms,
        )

    assert delayed_result[0].low == pytest.approx(685.77)
    assert iex_result == [iex]


def test_delete_bar_correction_restores_provider_bar(
    tmp_path: Path,
) -> None:
    raw = _bar(_SPY_SESSION, low=68.64)
    timestamp_ms = _timestamp_ms(_SPY_SESSION)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars("alpaca:delayed_sip", "SPY", Timeframe.DAILY, [raw])
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                low=685.77,
            )
        )

        cache.delete_bar_correction(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )
        restored = cache.get_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            timestamp_ms,
            timestamp_ms,
        )
        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )

    assert restored == [raw]
    assert inspection is not None
    assert inspection.correction is None


def test_correction_rejects_invalid_effective_ohlcv(
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION, low=68.64)],
        )

        with pytest.raises(ValueError, match="high"):
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=_SPY_SESSION,
                    high=680.0,
                )
            )

        with pytest.raises(ValueError, match="at least one"):
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=_SPY_SESSION,
                )
            )

        with pytest.raises(ValueError, match="volume"):
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=_SPY_SESSION,
                    volume=cast(Any, float("nan")),
                )
            )


def test_correction_rejects_unknown_bar(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        with pytest.raises(ValueError, match="does not exist"):
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=_SPY_SESSION,
                    low=685.77,
                )
            )


def test_correction_rejects_timezone_naive_timestamp(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        with pytest.raises(ValueError, match="timezone-aware"):
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=datetime(2026, 2, 2, 5, 0),
                    low=685.77,
                )
            )


def test_find_suspicious_bars_uses_symmetric_close_ratio(
    tmp_path: Path,
) -> None:
    normal_session = datetime(2026, 2, 3, 5, 0, tzinfo=UTC)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [
                _bar(_SPY_SESSION, low=68.64),
                _bar(normal_session, low=680.38, close=685.85),
            ],
        )

        candidates = cache.find_suspicious_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            minimum_deviation_percent=100.0,
        )
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace="alpaca:delayed_sip",
                symbol="SPY",
                timeframe=Timeframe.DAILY,
                timestamp=_SPY_SESSION,
                low=685.77,
            )
        )
        corrected_candidates = cache.find_suspicious_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            minimum_deviation_percent=100.0,
        )

    assert len(candidates) == 1
    assert candidates[0].inspection.raw_bar == _bar(_SPY_SESSION, low=68.64)
    assert candidates[0].deviation_percent == pytest.approx(
        (691.7 / 68.64 - 1.0) * 100.0
    )
    assert candidates[0].inspection.correction is None
    assert corrected_candidates[0].inspection.correction is not None
    assert corrected_candidates[0].inspection.effective_bar.low == pytest.approx(685.77)


def test_find_suspicious_bars_reports_nonpositive_prices(
    tmp_path: Path,
) -> None:
    invalid = Bar(
        timestamp=_SPY_SESSION,
        open=685.9,
        high=693.21,
        low=0.0,
        close=691.7,
        volume=79_286_521,
    )

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [invalid],
        )

        candidates = cache.find_suspicious_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            minimum_deviation_percent=100.0,
        )

    assert len(candidates) == 1
    assert candidates[0].deviation_percent == float("inf")


def test_find_suspicious_bars_does_not_query_each_bar_individually(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION, low=68.64)],
        )

        def fail_individual_lookup(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("suspicious-bar scan performed an N+1 lookup")

        monkeypatch.setattr(cache, "get_bar_inspection", fail_individual_lookup)

        candidates = cache.find_suspicious_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            minimum_deviation_percent=100.0,
        )

    assert len(candidates) == 1


def test_find_suspicious_bars_builds_inspections_only_for_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import data.cache as cache_module

    normal_session = datetime(2026, 2, 3, 5, 0, tzinfo=UTC)
    original = cache_module._row_to_bar_inspection
    inspection_calls = 0

    def count_inspection(*args: Any, **kwargs: Any) -> Any:
        nonlocal inspection_calls
        inspection_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cache_module, "_row_to_bar_inspection", count_inspection)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [
                _bar(_SPY_SESSION, low=68.64),
                _bar(normal_session, low=680.38, close=685.85),
            ],
        )

        candidates = cache.find_suspicious_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            minimum_deviation_percent=100.0,
        )

    assert len(candidates) == 1
    assert inspection_calls == 1


def test_get_bar_inspections_for_date_returns_only_requested_session(
    tmp_path: Path,
) -> None:
    first = datetime(2026, 2, 2, 14, 30, tzinfo=UTC)
    second = datetime(2026, 2, 2, 14, 45, tzinfo=UTC)
    next_day = datetime(2026, 2, 3, 14, 30, tzinfo=UTC)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.MIN15,
            [_bar(first), _bar(second), _bar(next_day)],
        )

        inspections = cache.get_bar_inspections_for_date(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.MIN15,
            date(2026, 2, 2),
        )

    assert [item.raw_bar.timestamp for item in inspections] == [first, second]


def test_get_bar_inspections_for_date_uses_new_york_calendar_date(
    tmp_path: Path,
) -> None:
    prior_session_after_hours = datetime(2026, 2, 2, 0, 45, tzinfo=UTC)
    requested_session_after_hours = datetime(2026, 2, 3, 0, 45, tzinfo=UTC)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.MIN15,
            [
                _bar(prior_session_after_hours),
                _bar(requested_session_after_hours),
            ],
        )

        inspections = cache.get_bar_inspections_for_date(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.MIN15,
            date(2026, 2, 2),
        )

    assert [item.raw_bar.timestamp for item in inspections] == [
        requested_session_after_hours
    ]


def test_daily_date_lookup_includes_yahoo_midnight_utc_bar(
    tmp_path: Path,
) -> None:
    yahoo_midnight = datetime(2026, 2, 2, 0, 0, tzinfo=UTC)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            [_bar(yahoo_midnight)],
        )

        inspections = cache.get_bar_inspections_for_date(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            date(2026, 2, 2),
        )

    assert [item.raw_bar.timestamp for item in inspections] == [yahoo_midnight]


def test_weekly_date_lookup_snaps_to_containing_week(
    tmp_path: Path,
) -> None:
    monday = datetime(2026, 2, 2, 0, 0, tzinfo=UTC)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "SPY",
            Timeframe.WEEKLY,
            [_bar(monday)],
        )

        inspections = cache.get_bar_inspections_for_date(
            "yfinance",
            "SPY",
            Timeframe.WEEKLY,
            date(2026, 2, 5),
        )

    assert [item.raw_bar.timestamp for item in inspections] == [monday]


def test_get_bar_cache_namespaces_lists_only_cached_data_sources(
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION)],
        )
        cache.put_bars(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION)],
        )

        namespaces = cache.get_bar_cache_namespaces()

    assert namespaces == ["alpaca:delayed_sip", "yfinance"]


def test_refresh_provider_bar_removes_correction_that_matches_new_raw_value(
    tmp_path: Path,
) -> None:
    timestamp_ms = _timestamp_ms(_SPY_SESSION)
    refreshed = _bar(_SPY_SESSION, low=685.77)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION, low=68.64)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            timestamp_ms - 86_400_000,
            timestamp_ms + 86_400_000,
        )
        original_coverage = cache.get_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
        )
        cache.put_bar_correction(
            BarCorrection(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.DAILY,
                _SPY_SESSION,
                low=685.77,
            )
        )

        cache.refresh_provider_bar(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
            refreshed,
        )
        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )
        refreshed_coverage = cache.get_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
        )

    assert inspection is not None
    assert inspection.raw_bar == refreshed
    assert inspection.correction is None
    assert refreshed_coverage == original_coverage


def test_refresh_provider_bar_normalizes_matching_fields_only(
    tmp_path: Path,
) -> None:
    refreshed = Bar(
        _SPY_SESSION,
        685.9,
        695.0,
        685.77,
        691.7,
        79_286_521,
        691.25,
    )
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION, low=68.64)],
        )
        cache.put_bar_correction(
            BarCorrection(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.DAILY,
                _SPY_SESSION,
                high=700.0,
                low=685.77,
            )
        )

        cache.refresh_provider_bar(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
            refreshed,
        )
        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )

    assert inspection is not None
    assert inspection.correction is not None
    assert inspection.correction.high == pytest.approx(700.0)
    assert inspection.correction.low is None


def test_refresh_provider_bar_retains_conflicting_override_for_review(
    tmp_path: Path,
) -> None:
    refreshed = Bar(_SPY_SESSION, 80.0, 90.0, 70.0, 80.0, 1_000)
    timestamp_ms = _timestamp_ms(_SPY_SESSION)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [Bar(_SPY_SESSION, 100.0, 110.0, 90.0, 100.0, 1_000)],
        )
        cache.put_bar_correction(
            BarCorrection(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.DAILY,
                _SPY_SESSION,
                low=95.0,
            )
        )

        cache.refresh_provider_bar(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
            refreshed,
        )
        inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )
        effective = cache.get_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            timestamp_ms,
            timestamp_ms,
        )

    assert inspection is not None
    assert inspection.correction is not None
    assert inspection.correction.low == pytest.approx(95.0)
    assert inspection.correction_error is not None
    assert effective == [refreshed]


def test_refresh_provider_bar_rekeys_daily_correction_when_timestamp_moves(
    tmp_path: Path,
) -> None:
    refreshed_timestamp = datetime(2026, 2, 2, 6, 0, tzinfo=UTC)
    refreshed = _bar(refreshed_timestamp, low=680.0)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION, low=679.0)],
        )
        cache.put_bar_correction(
            BarCorrection(
                "yfinance",
                "SPY",
                Timeframe.DAILY,
                _SPY_SESSION,
                low=685.77,
            )
        )

        cache.refresh_provider_bar(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
            refreshed,
        )
        old = cache.get_bar_inspection(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )
        moved = cache.get_bar_inspection(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            refreshed_timestamp,
        )

    assert old is None
    assert moved is not None
    assert moved.raw_bar == refreshed
    assert moved.correction is not None
    assert moved.correction.timestamp == refreshed_timestamp
    assert moved.correction.low == pytest.approx(685.77)


def test_refresh_provider_bar_rejects_shifted_intraday_timestamp_atomically(
    tmp_path: Path,
) -> None:
    original_timestamp = datetime(2026, 2, 2, 14, 30, tzinfo=UTC)
    shifted_timestamp = datetime(2026, 2, 2, 14, 31, tzinfo=UTC)
    original = _bar(original_timestamp, low=68.64)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.MIN15,
            [original],
        )
        cache.put_bar_correction(
            BarCorrection(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.MIN15,
                original_timestamp,
                low=685.77,
            )
        )

        with pytest.raises(ValueError, match="same bar session"):
            cache.refresh_provider_bar(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.MIN15,
                original_timestamp,
                _bar(shifted_timestamp),
            )
        original_inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.MIN15,
            original_timestamp,
        )
        shifted_inspection = cache.get_bar_inspection(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.MIN15,
            shifted_timestamp,
        )

    assert original_inspection is not None
    assert original_inspection.raw_bar == original
    assert original_inspection.correction is not None
    assert shifted_inspection is None


def test_refresh_provider_bar_rejects_timestamp_collision_without_changes(
    tmp_path: Path,
) -> None:
    colliding_timestamp = datetime(2026, 2, 2, 6, 0, tzinfo=UTC)
    original = _bar(_SPY_SESSION, low=68.64)
    collision = _bar(colliding_timestamp, low=680.0)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            [original, collision],
        )
        cache.put_bar_correction(
            BarCorrection(
                "yfinance",
                "SPY",
                Timeframe.DAILY,
                _SPY_SESSION,
                low=685.77,
            )
        )

        with pytest.raises(ValueError, match="already has a cached bar"):
            cache.refresh_provider_bar(
                "yfinance",
                "SPY",
                Timeframe.DAILY,
                _SPY_SESSION,
                _bar(colliding_timestamp, low=681.0),
            )
        original_inspection = cache.get_bar_inspection(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            _SPY_SESSION,
        )
        collision_inspection = cache.get_bar_inspection(
            "yfinance",
            "SPY",
            Timeframe.DAILY,
            colliding_timestamp,
        )

    assert original_inspection is not None
    assert original_inspection.raw_bar == original
    assert original_inspection.correction is not None
    assert collision_inspection is not None
    assert collision_inspection.raw_bar == collision


def _bar(
    timestamp: datetime,
    *,
    low: float = 685.77,
    close: float = 691.7,
    volume: int = 79_286_521,
) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=685.9,
        high=693.21,
        low=low,
        close=close,
        volume=volume,
        vwap=691.25,
    )


def _timestamp_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)
