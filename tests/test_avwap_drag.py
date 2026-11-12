from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

import simplechart.extensions._series_arrays as series_arrays_module
from data.models import Timeframe
from indicators.avwap import AVWAPIndicator, avwap_anchor_key
from indicators.avwap.models import AnchorRecord
from simplechart.api import (
    Bar,
    ChartEvent,
    DragSession,
    OHLCVSeries,
    series_arrays,
)

_FIRST = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def _series(count: int = 300, timeframe: Timeframe = Timeframe.MIN5, step: timedelta = timedelta(minutes=5)) -> OHLCVSeries:
    return OHLCVSeries(
        "XBI",
        timeframe,
        [
            Bar(
                _FIRST + step * i,
                100.0 + (i % 7),
                101.5 + (i % 7),
                99.0 + (i % 5),
                100.5 + (i % 3),
                0 if i in (40, 41) else 1_000 + 37 * (i % 11),
            )
            for i in range(count)
        ],
    )


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _anchor(anchor_ts: int, anchor_id: int = 1) -> AnchorRecord:
    return AnchorRecord(
        symbol="XBI",
        anchor_ts=anchor_ts,
        label="test",
        color="#00FF88",
        show_anchor=True,
        anchor_id=anchor_id,
    )


def _reference_avwap(series: OHLCVSeries, start: int) -> np.ndarray:
    # Independent running-sum definition: cumulative typical price x volume
    # over cumulative volume from the anchor bar; NaN until volume is seen.
    out = np.full(len(series.bars), np.nan)
    tp_v = 0.0
    vol = 0.0
    for i in range(start, len(series.bars)):
        bar = series.bars[i]
        tp_v += (bar.high + bar.low + bar.close) / 3.0 * bar.volume
        vol += bar.volume
        if vol > 0:
            out[i] = tp_v / vol
    return out


# --- series_arrays -----------------------------------------------------------


def test_series_arrays_match_the_bars_and_are_read_only() -> None:
    series = _series(20)

    arrays = series_arrays(series)

    assert arrays.timestamps_ms.tolist() == [_ms(bar.timestamp) for bar in series.bars]
    assert arrays.open.tolist() == [bar.open for bar in series.bars]
    assert arrays.high.tolist() == [bar.high for bar in series.bars]
    assert arrays.low.tolist() == [bar.low for bar in series.bars]
    assert arrays.close.tolist() == [bar.close for bar in series.bars]
    assert arrays.volume.tolist() == [float(bar.volume) for bar in series.bars]
    with pytest.raises(ValueError):
        arrays.close[0] = 0.0


def test_series_arrays_are_built_once_per_series_and_rebuilt_when_bars_change() -> None:
    series = _series(20)

    first = series_arrays(series)
    assert series_arrays(series) is first

    series.bars = series.bars[:10]
    rebuilt = series_arrays(series)
    assert rebuilt is not first
    assert len(rebuilt.close) == 10


def test_series_arrays_are_released_with_their_series() -> None:
    import gc

    series = _series(20)
    series_arrays(series)
    key = id(series)
    assert key in series_arrays_module._cache

    del series
    gc.collect()

    assert key not in series_arrays_module._cache


# --- AVWAP values --------------------------------------------------------------


@pytest.mark.parametrize(
    ("anchor_ts", "start"),
    [
        (_ms(_FIRST) - 3_600_000, 0),  # before the first bar
        (_ms(_FIRST + timedelta(minutes=5 * 37)), 37),  # exactly on a bar; zero volume at 40-41
        (_ms(_FIRST + timedelta(minutes=5 * 120 + 2)), 120),  # inside a bar
    ],
)
def test_avwap_values_equal_the_running_sum_definition(anchor_ts: int, start: int) -> None:
    series = _series()
    anchor = _anchor(anchor_ts)

    values = AVWAPIndicator().compute(series, {"anchors": [anchor]})[avwap_anchor_key(anchor)]

    np.testing.assert_allclose(values, _reference_avwap(series, start), rtol=1e-12, equal_nan=True)


def test_anchor_newer_than_the_loaded_bars_is_not_drawn() -> None:
    series = _series()
    anchor = _anchor(_ms(_FIRST + timedelta(minutes=5 * 400)))

    values = AVWAPIndicator().compute(series, {"anchors": [anchor]})[avwap_anchor_key(anchor)]

    assert np.isnan(values).all()


def test_several_anchors_are_computed_independently() -> None:
    series = _series()
    early = _anchor(_ms(_FIRST + timedelta(minutes=5 * 10)), anchor_id=1)
    late = _anchor(_ms(_FIRST + timedelta(minutes=5 * 200)), anchor_id=2)

    result = AVWAPIndicator().compute(series, {"anchors": [early, late]})

    np.testing.assert_allclose(result[avwap_anchor_key(early)], _reference_avwap(series, 10), rtol=1e-12, equal_nan=True)
    np.testing.assert_allclose(result[avwap_anchor_key(late)], _reference_avwap(series, 200), rtol=1e-12, equal_nan=True)


# --- grabbing the anchor ------------------------------------------------------------


def _hit(series: OHLCVSeries, anchor: AnchorRecord, x: float, y: float, bars_per_pixel: float) -> bool:
    nearest = int(round(x))
    event = ChartEvent(
        x=x,
        y=y,
        bar_index=nearest if abs(x - nearest) <= 0.75 else None,
        timestamp_ms=None,
        pixel_size_x=bars_per_pixel,
        pixel_size_y=0.01,
    )
    hit = AVWAPIndicator().hit_test(series, {"anchors": [anchor]}, event, {avwap_anchor_key(anchor)})
    return hit is not None


@pytest.mark.parametrize(
    ("bars_per_pixel", "offset_px", "expected"),
    [
        (0.45, 5, True),  # zoomed out (~2.2 px per bar): 5 px is 2.25 bars away
        (0.45, 8, False),  # beyond the 6 px grab radius
        (0.1, 7, True),  # zoomed in (10 px per bar): within the 0.75-bar minimum
        (0.1, 10, False),  # one whole bar away
    ],
)
def test_anchor_grab_radius_is_measured_in_pixels(bars_per_pixel: float, offset_px: int, expected: bool) -> None:
    series = _series()
    anchor_index = 150
    anchor = _anchor(_ms(series.bars[anchor_index].timestamp))
    bar = series.bars[anchor_index]

    hit = _hit(series, anchor, anchor_index + offset_px * bars_per_pixel, bar.close, bars_per_pixel)

    assert hit is expected


def test_anchor_inside_a_coarser_bar_is_grabbed_where_its_line_starts() -> None:
    daily = _series(30, Timeframe.DAILY, timedelta(days=1))
    # A 5m anchor at 15:00 on day 10 is drawn from day 10's bar.
    anchor = _anchor(_ms(daily.bars[10].timestamp + timedelta(minutes=30)))
    values = AVWAPIndicator().compute(daily, {"anchors": [anchor]})[avwap_anchor_key(anchor)]
    assert np.isnan(values[9]) and not np.isnan(values[10])

    assert _hit(daily, anchor, 10.0, daily.bars[10].close, 0.05)
    assert not _hit(daily, anchor, 11.0, daily.bars[11].close, 0.05)


# --- drag frames -----------------------------------------------------------------------


def test_drag_frames_reuse_the_series_arrays(monkeypatch: pytest.MonkeyPatch) -> None:
    series = _series()
    anchor = _anchor(_ms(series.bars[150].timestamp))
    extension = AVWAPIndicator()
    session = DragSession(
        extension_name="avwap",
        handle_key=avwap_anchor_key(anchor),
        original_params={"anchors": [anchor]},
        working_params={"anchors": [anchor]},
    )
    builds: list[int] = []
    production_build = series_arrays_module._build

    def counting_build(target: OHLCVSeries) -> object:
        builds.append(len(target.bars))
        return production_build(target)

    monkeypatch.setattr(series_arrays_module, "_build", counting_build)

    for target in range(150, 170):
        extension.drag_to(series, session, ChartEvent(float(target), 100.0, target, None, pixel_size_x=0.45))

    assert builds == [len(series.bars)]


def test_anchor_bar_lookup_follows_the_api_timestamp_rule() -> None:
    from indicators.avwap import _anchor_bar_index
    from simplechart.api import timestamp_ms_to_bar_index

    series = _series(10)
    timestamps = [_ms(bar.timestamp) for bar in series.bars]
    step = timestamps[1] - timestamps[0]
    probes = [
        timestamps[0] - 1,
        timestamps[0],
        timestamps[3] + 1,
        timestamps[-1],
        timestamps[-1] + step // 2,
        timestamps[-1] + step,
        timestamps[-1] + 10 * step,
    ]

    arrays = series_arrays(series)
    assert [_anchor_bar_index(arrays.timestamps_ms, probe) for probe in probes] == [
        timestamp_ms_to_bar_index(probe, timestamps) for probe in probes
    ]


def test_an_anchor_cannot_be_dropped_onto_the_bar_another_line_starts_on() -> None:
    daily = _series(30, Timeframe.DAILY, timedelta(days=1))
    # Drawn from day 10's bar (the bar containing 30 minutes after its open).
    existing = _anchor(_ms(daily.bars[10].timestamp + timedelta(minutes=30)), anchor_id=1)
    moving = _anchor(_ms(daily.bars[20].timestamp), anchor_id=2)
    extension = AVWAPIndicator()
    session = DragSession(
        extension_name="avwap",
        handle_key=avwap_anchor_key(moving),
        original_params={"anchors": [existing, moving]},
        working_params={"anchors": [existing, moving]},
    )

    blocked = extension.drag_to(daily, session, ChartEvent(10.0, 100.0, 10, None))
    allowed = extension.drag_to(daily, session, ChartEvent(11.0, 100.0, 11, None))

    assert blocked is None
    assert allowed is not None


# AVWAP anchors far back on intraday charts (History wave, user request
# 2026-11-11): anchors more than 600 days before the newest bar are not
# drawn on intraday charts (they would load years of intraday bars), do not
# ask for older history, and explain why in a notice.


def test_an_intraday_anchor_beyond_the_span_is_hidden_with_a_notice() -> None:
    series = _series()
    newest = series.bars[-1].timestamp
    far = _anchor(_ms(newest - timedelta(days=601)), anchor_id=1)
    near = _anchor(_ms(series.bars[10].timestamp), anchor_id=2)

    render = AVWAPIndicator().render(
        series, {"anchors": [far, near], "_history_complete": False},
    )

    assert [line.key for line in render.series] == [avwap_anchor_key(near)]
    assert render.pending_history_start_ms is None
    assert render.notice is not None and "600 days" in render.notice


def test_an_intraday_anchor_within_the_span_waits_for_older_history() -> None:
    series = _series()
    within = _anchor(_ms(series.bars[-1].timestamp - timedelta(days=599)))

    render = AVWAPIndicator().render(
        series, {"anchors": [within], "_history_complete": False},
    )

    assert render.series == []
    assert render.pending_history_start_ms == within.anchor_ts
    assert render.notice is None


def test_a_daily_anchor_far_back_is_still_drawn() -> None:
    daily = _series(30, Timeframe.DAILY, timedelta(days=1))
    far = _anchor(_ms(daily.bars[-1].timestamp - timedelta(days=2000)))

    render = AVWAPIndicator().render(daily, {"anchors": [far], "_history_complete": True})

    assert [line.key for line in render.series] == [avwap_anchor_key(far)]
    assert render.notice is None
