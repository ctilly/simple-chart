"""
app/header_bar.py

Controlled in-app header for Simple Chart.

The native OS title bar is intentionally left alone because its styling is not
consistent across Linux, macOS, and Windows. This header gives the app a visible
cross-platform identity band inside the Qt content area.

The left side is a Level 1 quote strip for the active chart symbol: company
name, last price, change, bid/ask with lot sizes, session OHLV, and previous
close, followed by the next earnings date.
"""

from html import escape

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from app.theme import active_colors
from data.models import Level1Quote


_GAP = "&nbsp;&nbsp;&nbsp;"


class AppHeader(QFrame):
    """Level 1 quote strip and upcoming earnings."""

    details_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        self.setObjectName("appHeader")
        self.setFixedHeight(24)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        self._quote_label = QLabel()
        self._quote_label.setObjectName("level1Quote")
        self._quote_label.setTextFormat(Qt.TextFormat.RichText)
        self._quote_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        self._quote_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._quote_label.setOpenExternalLinks(False)
        self._quote_label.linkActivated.connect(self._activate_link)
        self._quote_label.setToolTip(
            "Next earnings source: Yahoo Finance. Dates may change."
        )
        layout.addWidget(self._quote_label)
        self._quote_html = ""
        self.set_earnings_date("—")
        layout.addStretch(1)

    def set_earnings_date(self, date_text: str, *, highlight: bool = False) -> None:
        if highlight:
            date_text = (
                '<span style="background-color: #ffdc58; color: #251c00;">'
                f"{date_text}</span>"
            )
        self._earnings_html = _field("Next earnings", date_text)
        self._render_quote_strip()

    def set_symbol(self, symbol: str) -> None:
        """Show the bare symbol while its quote is being fetched."""
        self._quote_html = _details_link(symbol, None)
        self._render_quote_strip()

    def set_quote(self, quote: Level1Quote) -> None:
        """Render the full Level 1 strip for the active symbol."""
        self._quote_html = _level1_html(quote)
        self._render_quote_strip()

    def _render_quote_strip(self) -> None:
        self._quote_label.setText(self._quote_html + self._earnings_html)

    def _activate_link(self, link: str) -> None:
        if link == "symbol-details":
            self.details_requested.emit()


def _details_link(symbol: str, name: str | None) -> str:
    colors = active_colors()
    text = (
        f'<span style="color: {colors["header_symbol"]}; font-weight: bold;">{escape(symbol)}</span>'
    )
    if name:
        text += (
            f'{_GAP}<span style="color: {colors["header_name"]};">{escape(name)}</span>'
        )
    return f'<a href="symbol-details" style="text-decoration: none;">{text}</a>'


def _level1_html(quote: Level1Quote) -> str:
    colors = active_colors()
    parts = [_details_link(quote.symbol, quote.company_name)]
    parts.append(
        f'{_GAP}<span style="color: {colors["header_value"]}; font-weight: bold;">'
        f"{_price(quote.last_price)}</span>"
    )
    if quote.change is not None:
        parts.append(f'&nbsp;<span style="color: {_change_color(quote.change)};">'
                     f"{_change_text(quote.change, quote.change_percent)}</span>")

    parts.append(_field("Bid", _quote_side(quote.bid, quote.bid_size)))
    parts.append(_field("Ask", _quote_side(quote.ask, quote.ask_size)))
    parts.append(_field("O", _price(quote.open)))
    parts.append(_field("H", _price(quote.high)))
    parts.append(_field("L", _price(quote.low)))
    parts.append(_field("V", _volume(quote.volume)))
    parts.append(_field("PC", _price(quote.previous_close)))
    return "".join(parts)


def _field(label: str, value: str) -> str:
    colors = active_colors()
    return (
        f'{_GAP}<span style="color: {colors["header_label"]};">{label}</span>'
        f'&nbsp;<span style="color: {colors["header_value"]};">{value}</span>'
    )


def _price(value: float | None) -> str:
    return "--" if value is None else f"{value:,.2f}"


def _quote_side(price: float | None, size: int | None) -> str:
    if price is None:
        return "--"
    text = _price(price)
    if size is not None:
        text += f"&times;{size}"
    return text


def _volume(value: int | None) -> str:
    if value is None:
        return "--"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return str(value)


def _change_color(change: float) -> str:
    colors = active_colors()
    if change > 0:
        return colors["candle_up"]
    if change < 0:
        return colors["candle_down"]
    return colors["header_name"]


def _change_text(change: float, change_percent: float | None) -> str:
    sign = "+" if change > 0 else ""
    text = f"{sign}{change:,.2f}"
    if change_percent is not None:
        text += f" ({sign}{change_percent:.2f}%)"
    return text
