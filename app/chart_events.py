from datetime import date, timezone

from app.earnings_worker import HistoricalEventDates
from app.theme import active_colors
from data.calendar import session_date_anchor
from data.session import MARKET_TIMEZONE
from simplechart.api import MarkerRender, OHLCVSeries


CHART_EVENT_PREFIX = "@chart-event:"


def chart_event_markers(
    series: OHLCVSeries, history: HistoricalEventDates, visibility: tuple[bool, bool],
) -> list[MarkerRender]:
    by_bar_day: dict[date, list[str]] = {}
    for label, dates, enabled in zip(("E", "D"), history, visibility, strict=True):
        if enabled:
            for day in dates:
                anchor = session_date_anchor(day, series.timeframe)
                by_bar_day.setdefault(anchor, []).append(label)
    if not by_bar_day:
        return []

    timezone_for_bar = MARKET_TIMEZONE if series.timeframe.is_intraday else timezone.utc
    color = active_colors()["foreground"]
    markers: list[MarkerRender] = []
    for index, bar in enumerate(series.bars):
        bar_day = bar.timestamp.astimezone(timezone_for_bar).date()
        labels = by_bar_day.pop(session_date_anchor(bar_day, series.timeframe), None)
        if labels is None:
            continue
        text = " ".join(
            label if (count := labels.count(label)) == 1 else f"{label}×{count}"
            for label in ("E", "D")
            if label in labels
        )
        markers.append(MarkerRender(
            f"{CHART_EVENT_PREFIX}{bar_day.isoformat()}", float(index), bar.high,
            text, color, font_size=9, anchor=(0.5, 1.0),
        ))
        if not by_bar_day:
            break
    return markers
