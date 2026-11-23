from datetime import datetime
from html import escape
from typing import Literal

from PyQt6.QtCore import QObject, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QLabel, QTabWidget, QTextBrowser,
    QWidget,
)

from app.dialogs import build_dialog_content
from app.fund_calendar import QQQ_REBALANCE_DATES, WITCHING_DATES, next_calendar_date
from data.models import Level1Quote
from data.provider.symbol_details import NewsArticle, SymbolProfile, fetch_news, fetch_profile
from data.session import MARKET_TIMEZONE


_Section = Literal["profile", "news"]


def _label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _value(number: int | float | None, spec: str) -> str:
    return "—" if number is None else format(number, spec)


class SymbolDetailsDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.resize(620, 520)
        self.symbol = ""
        layout = build_dialog_content(self, "symbolDetails", "Symbol Details")
        self.setSizeGripEnabled(True)
        self._title = _label()
        self._title.setObjectName("symbolDetailsTitle")
        layout.addWidget(self._title)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)
        quote_tab = QWidget()
        quote_layout = QFormLayout(quote_tab)
        self._quote_status = _label()
        quote_layout.addRow(self._quote_status)
        self._quote_values: list[QLabel] = []
        for name in (
            "Last price", "Change", "Change (%)", "Bid", "Bid size (lots)",
            "Ask", "Ask size (lots)", "Bid–ask spread", "Open", "High", "Low",
            "Volume", "Previous close",
        ):
            label = _label()
            quote_layout.addRow(name, label)
            self._quote_values.append(label)
        self._tabs.addTab(quote_tab, "Quote")

        self._profile = QTextBrowser()
        self._profile.setOpenLinks(False)
        self._tabs.addTab(self._profile, "Company / Fund")

        self._news = QTextBrowser()
        self._news.setObjectName("symbolDetailsNews")
        self._news.setOpenLinks(False)
        self._news.anchorClicked.connect(self._open_article)
        self._tabs.addTab(self._news, "Latest news")

    def reset(self, symbol: str) -> None:
        self.symbol = symbol
        self._set_name(None)
        self._tabs.setCurrentIndex(0)
        self._profile.setPlainText("Loading Yahoo Finance profile…")
        self._news.setPlainText("Loading Yahoo Finance news…")
        self.set_quote(None)

    def _set_name(self, name: str | None) -> None:
        title = f"{self.symbol} · {name}" if name else self.symbol
        self._title.setText(title)
        self.setWindowTitle(f"Symbol details · {title}")

    def set_quote(self, quote: Level1Quote | None) -> None:
        if quote is None:
            for label in self._quote_values:
                label.setText("—")
            self._quote_status.setText("Waiting for Level 1 quote")
            return
        if quote.company_name:
            self._set_name(quote.company_name)
        spread = None if quote.ask is None or quote.bid is None else quote.ask - quote.bid
        values = (
            _value(quote.last_price, ",.2f"), _value(quote.change, "+,.2f"),
            _value(quote.change_percent, "+,.2f"), _value(quote.bid, ",.2f"),
            _value(quote.bid_size, ",d"), _value(quote.ask, ",.2f"),
            _value(quote.ask_size, ",d"), _value(spread, ",.2f"), _value(quote.open, ",.2f"),
            _value(quote.high, ",.2f"), _value(quote.low, ",.2f"),
            _value(quote.volume, ",d"), _value(quote.previous_close, ",.2f"),
        )
        for label, value in zip(self._quote_values, values, strict=True):
            label.setText(value)
        self._quote_status.setText("Latest Level 1 quote")

    def retain_quote(self) -> None:
        if self._quote_status.text() == "Latest Level 1 quote":
            self._quote_status.setText("Last received Level 1 quote")

    def set_profile(self, profile: SymbolProfile | None) -> None:
        if profile is None:
            self._profile.setPlainText("Yahoo Finance profile unavailable.")
            return
        if profile.name:
            self._set_name(profile.name)
        description = escape(profile.description or "Description unavailable.")
        if not profile.is_etf:
            self._profile.setHtml(
                f"<p><b>Share float</b> {_value(profile.float_shares, ',d')}</p>"
                f"<p>{description}</p><p>Source: Yahoo Finance</p>"
            )
            return
        today = datetime.now(MARKET_TIMEZONE).date()
        rows = [
            ("Issuer", profile.issuer or "—"),
            ("Benchmark", profile.benchmark or "—"),
            ("Management fee", _value(profile.management_fee, ".2%")),
            ("Annual report expense ratio", _value(profile.expense_ratio, ".2%")),
            ("Next U.S. witching date", next_calendar_date(WITCHING_DATES, today)),
        ]
        if self.symbol == "QQQ":
            rows.extend((
                ("Nasdaq-100 schedule", "Quarterly rebalance, annual December reconstitution"),
                ("Next scheduled effective date", next_calendar_date(QQQ_REBALANCE_DATES, today)),
            ))
        else:
            rows.append(("Fund rebalance / reconstitution", "Schedule unavailable"))
        body = "".join(f"<p><b>{key}</b> {escape(value)}</p>" for key, value in rows)
        holdings = "".join(
            f"<tr><td>{escape(item.symbol)}</td><td>{escape(item.name)}</td>"
            f"<td>{item.weight:.2%}</td></tr>" for item in profile.holdings
        )
        table = (
            f"<h3>Top holdings ({len(profile.holdings)})</h3>"
            f"<table cellspacing='7'><tr><th>Symbol</th><th>Name</th><th>Weight</th></tr>"
            f"{holdings}</table>" if holdings else "<p>Holdings unavailable.</p>"
        )
        self._profile.setHtml(
            f"<p>{description}</p>{body}{table}"
            "<p>Fund data: Yahoo Finance. Calendar: Cboe, OCC and Nasdaq.</p>"
        )

    def set_news(self, articles: tuple[NewsArticle, ...] | None) -> None:
        if not articles:
            self._news.setPlainText(
                "Yahoo Finance news unavailable." if articles is None
                else "Yahoo Finance returned no recent headlines."
            )
            return
        entries: list[str] = []
        for article in articles:
            headline = escape(article.headline)
            if article.url is not None:
                headline = f'<a href="{escape(article.url, quote=True)}">{headline}</a>'
            details = [article.publisher] if article.publisher else []
            if article.published:
                details.append(article.published.astimezone(MARKET_TIMEZONE).strftime("%b %d, %Y · %H:%M ET"))
            entries.append(f"<p><b>{headline}</b><br>{escape(' · '.join(details))}</p>")
        self._news.setHtml("".join(entries))

    def _open_article(self, url: QUrl) -> None:
        if url.isValid() and url.scheme() in {"http", "https"} and url.host() and not url.userInfo():
            QDesktopServices.openUrl(url)


class _DetailsThread(QThread):
    result = pyqtSignal(int, str, object)

    def __init__(self, section: _Section, symbol: str, generation: int) -> None:
        super().__init__()
        self.section = section
        self._symbol = symbol
        self._generation = generation

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        result = fetch_profile(self._symbol) if self.section == "profile" else fetch_news(self._symbol)
        if not self.isInterruptionRequested():
            self.result.emit(self._generation, self.section, result)


class SymbolDetails(QObject):
    idle = pyqtSignal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self.dialog: SymbolDetailsDialog | None = None
        self._generation = 0
        self._threads: dict[_Section, _DetailsThread] = {}
        self._pending: dict[_Section, tuple[str, int]] = {}

    @property
    def busy(self) -> bool:
        return bool(self._threads)

    def show(self, symbol: str, quote: Level1Quote | None) -> None:
        if self.dialog is None:
            self.dialog = SymbolDetailsDialog(self._parent)
            self.dialog.finished.connect(self._invalidate)
        if not self.dialog.isVisible() or self.dialog.symbol != symbol:
            self._invalidate()
            self.dialog.reset(symbol)
            self.dialog.set_quote(quote)
            for section in ("profile", "news"):
                self._pending[section] = (symbol, self._generation)
                self._start(section)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def update_quote(self, quote: Level1Quote) -> None:
        if self.dialog is not None and self.dialog.isVisible() and self.dialog.symbol == quote.symbol:
            self.dialog.set_quote(quote)

    def retain_quote(self) -> None:
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.retain_quote()

    def stop(self) -> None:
        self._invalidate()
        if self.dialog is not None:
            self.dialog.close()

    def _invalidate(self) -> None:
        self._generation += 1
        self._pending.clear()
        for thread in self._threads.values():
            thread.requestInterruption()

    def _start(self, section: _Section) -> None:
        if section in self._threads or section not in self._pending:
            return
        symbol, generation = self._pending.pop(section)
        thread = _DetailsThread(section, symbol, generation)
        self._threads[section] = thread
        thread.result.connect(self._on_result)
        thread.finished.connect(self._on_finished)
        thread.start()

    def _on_result(
        self, generation: int, section: _Section,
        value: SymbolProfile | tuple[NewsArticle, ...] | None,
    ) -> None:
        if generation != self._generation or self.dialog is None or not self.dialog.isVisible():
            return
        if section == "profile" and (value is None or isinstance(value, SymbolProfile)):
            self.dialog.set_profile(value)
        elif section == "news" and (value is None or isinstance(value, tuple)):
            self.dialog.set_news(value)

    def _on_finished(self) -> None:
        thread = self.sender()
        assert isinstance(thread, _DetailsThread)
        del self._threads[thread.section]
        thread.deleteLater()
        self._start(thread.section)
        if not self._threads:
            self.idle.emit()
