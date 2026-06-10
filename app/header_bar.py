"""
app/header_bar.py

Controlled in-app header for Simple Chart.

The native OS title bar is intentionally left alone because its styling is not
consistent across Linux, macOS, and Windows. This header gives the app a visible
cross-platform identity band inside the Qt content area.

The left side is a Level 1 quote strip for the active chart symbol: company
name, last price, change, bid/ask with lot sizes, session OHLV, and previous
close. The right side keeps the keyboard shortcut hints.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from chart.styles import CANDLE_DOWN, CANDLE_UP
from data.models import Level1Quote


_SHORTCUT_MESSAGES: list[str] = [
    "<b>Ctrl+r</b>: Reset Chart View",
    "<b>Esc</b>: Cancel Drawing",
]

# Level 1 strip palette, tuned for the dark header band.
_SYMBOL_COLOR = "#ffffff"
_NAME_COLOR   = "#c2ccd3"
_LABEL_COLOR  = "#aab5bd"
_VALUE_COLOR  = "#d7dde1"
_FLAT_COLOR   = "#c2ccd3"

_GAP = "&nbsp;&nbsp;&nbsp;"


class AppHeader(QFrame):
    """Level 1 quote strip and application shortcut hints."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        self.setObjectName("appHeader")
        self.setFixedHeight(24)
        self.setStyleSheet(
            "#appHeader {"
            " background: #2f3437;"
            " border-bottom: 1px solid #1f2427;"
            "}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(12)

        self._quote_label = QLabel()
        self._quote_label.setTextFormat(Qt.TextFormat.RichText)
        self._quote_label.setStyleSheet("font-size: 12px;")
        layout.addWidget(self._quote_label)

        layout.addStretch(1)

        shortcuts = QLabel("&nbsp;&nbsp;&nbsp;".join(_SHORTCUT_MESSAGES))
        shortcuts.setTextFormat(Qt.TextFormat.RichText)
        shortcuts.setStyleSheet("color: #d7dde1; font-size: 12px;")
        layout.addWidget(shortcuts)

    def set_symbol(self, symbol: str) -> None:
        """Show the bare symbol while its quote is being fetched."""
        self._quote_label.setText(
            f'<span style="color: {_SYMBOL_COLOR}; font-weight: bold;">{symbol}</span>'
        )

    def set_quote(self, quote: Level1Quote) -> None:
        """Render the full Level 1 strip for the active symbol."""
        self._quote_label.setText(_level1_html(quote))


def _level1_html(quote: Level1Quote) -> str:
    parts: list[str] = [
        f'<span style="color: {_SYMBOL_COLOR}; font-weight: bold;">{quote.symbol}</span>'
    ]
    if quote.company_name is not None:
        parts.append(
            f'&nbsp;&nbsp;&nbsp;<span style="color: {_NAME_COLOR};">{quote.company_name}</span>'
        )

    parts.append(
        f'{_GAP}<span style="color: {_VALUE_COLOR}; font-weight: bold;">'
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
    return (
        f'{_GAP}<span style="color: {_LABEL_COLOR};">{label}</span>'
        f'&nbsp;<span style="color: {_VALUE_COLOR};">{value}</span>'
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
    if change > 0:
        return CANDLE_UP
    if change < 0:
        return CANDLE_DOWN
    return _FLAT_COLOR


def _change_text(change: float, change_percent: float | None) -> str:
    sign = "+" if change > 0 else ""
    text = f"{sign}{change:,.2f}"
    if change_percent is not None:
        text += f" ({sign}{change_percent:.2f}%)"
    return text
