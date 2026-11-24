from datetime import datetime
from html import escape
from threading import Event
from typing import cast

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QLabel, QTabWidget, QTextBrowser,
    QWidget,
)

from app.background_job import LatestJob
from app.dialogs import build_dialog_content
from app.fund_calendar import next_qqq_rebalance_date, next_witching_date
from data.models import Level1Quote, NewsArticle, SymbolProfile
from data.provider.routing import CapabilityRouter, DataRoute
from data.provider.spec import Capability
from data.session import MARKET_TIMEZONE


def _label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _value(number: int | float | None, spec: str) -> str:
    return "—" if number is None else format(number, spec)


def _published_in_market_time(published: datetime | None) -> datetime | None:
    if published is None:
        return None
    try:
        return published.astimezone(MARKET_TIMEZONE)
    except (OverflowError, ValueError):
        return None


class SymbolDetailsDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.resize(620, 520)
        self.symbol = ""
        self._company_name: str | None = None
        self._profile_name: str | None = None
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
        self._company_name = self._profile_name = None
        self._update_name()
        self._tabs.setCurrentIndex(0)
        self.set_quote(None)

    def set_company_name(self, name: str | None) -> None:
        self._company_name = name
        self._update_name()

    def _update_name(self) -> None:
        name = self._company_name or self._profile_name
        title = f"{self.symbol} · {name}" if name else self.symbol
        self._title.setText(title)
        self.setWindowTitle(f"Symbol details · {title}")

    def set_quote(self, quote: Level1Quote | None) -> None:
        if quote is None:
            for label in self._quote_values:
                label.setText("—")
            self._quote_status.setText("Waiting for Level 1 quote")
            return
        self.set_company_name(quote.company_name)
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

    def load_profile(self, source: str) -> None:
        self._profile_name = None
        self._update_name()
        self._profile.setPlainText(f"Loading {source} profile…")

    def load_news(self, source: str) -> None:
        self._tabs.setTabToolTip(self._tabs.indexOf(self._news), f"<p>Source: {escape(source)}</p>")
        self._news.setPlainText(f"Loading {source} news…")

    def set_profile(self, profile: SymbolProfile | None, source: str) -> None:
        if profile is None:
            self._profile.setPlainText(f"{source} profile unavailable.")
            return
        self._profile_name = profile.name
        self._update_name()
        description = escape(profile.description or "Description unavailable.")
        if not profile.is_etf:
            self._profile.setHtml(
                f"<p><b>Share float</b> {_value(profile.float_shares, ',d')}</p>"
                f"<p>{description}</p><p>Source: {escape(source)}</p>"
            )
            return
        today = datetime.now(MARKET_TIMEZONE).date()
        rows = [
            ("Issuer", profile.issuer or "—"),
            ("Benchmark", profile.benchmark or "—"),
            ("Management fee", _value(profile.management_fee, ".2%")),
            ("Annual report expense ratio", _value(profile.expense_ratio, ".2%")),
            ("Next U.S. witching date", next_witching_date(today).strftime("%b %d, %Y")),
        ]
        if self.symbol == "QQQ":
            rows.extend((
                ("Nasdaq-100 schedule", "Quarterly rebalance, annual December reconstitution"),
                ("Next scheduled effective date", next_qqq_rebalance_date(today).strftime("%b %d, %Y")),
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
            f"<p>Fund data: {escape(source)}. Calendar: Cboe, OCC and Nasdaq.</p>"
        )

    def set_news(self, articles: tuple[NewsArticle, ...] | None, source: str) -> None:
        if not articles:
            self._news.setPlainText(
                f"{source} news unavailable." if articles is None
                else f"{source} returned no recent headlines."
            )
            return
        entries: list[str] = []
        for article in articles:
            headline = escape(article.headline)
            if article.url is not None:
                headline = f'<a href="{escape(article.url, quote=True)}">{headline}</a>'
            details = [article.publisher] if article.publisher else []
            published = _published_in_market_time(article.published)
            if published is not None:
                details.append(published.strftime("%b %d, %Y · %H:%M ET"))
            entries.append(f"<p><b>{headline}</b><br>{escape(' · '.join(details))}</p>")
        self._news.setHtml("".join(entries))

    def _open_article(self, url: QUrl) -> None:
        if url.isValid() and url.scheme() in {"http", "https"} and url.host() and not url.userInfo():
            QDesktopServices.openUrl(url)


class SymbolDetails(QObject):
    idle = pyqtSignal()

    def __init__(self, router: CapabilityRouter, parent: QWidget) -> None:
        super().__init__(parent)
        self._router = router
        self._profile_key: tuple[DataRoute, str] | None = None
        self._news_key: tuple[DataRoute, str] | None = None
        self._parent = parent
        self.dialog: SymbolDetailsDialog | None = None
        self._profile_job = LatestJob("Loading profile", self)
        self._profile_job.succeeded.connect(self._on_profile)
        self._profile_job.failed.connect(self._on_profile)
        self._profile_job.idle.connect(self._on_idle)
        self._news_job = LatestJob("Loading news", self)
        self._news_job.succeeded.connect(self._on_news)
        self._news_job.failed.connect(self._on_news)
        self._news_job.idle.connect(self._on_idle)

    @property
    def busy(self) -> bool:
        return self._profile_job.busy or self._news_job.busy

    def show(self, symbol: str, quote: Level1Quote | None) -> None:
        if self.dialog is None:
            self.dialog = SymbolDetailsDialog(self._parent)
            self.dialog.finished.connect(self._invalidate)
        if not self.dialog.isVisible() or self.dialog.symbol != symbol:
            self._invalidate()
            self.dialog.reset(symbol)
            self.dialog.set_quote(quote)

        self.dialog.show()
        self.refresh_routes()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def refresh_routes(self) -> None:
        dialog = self.dialog
        if dialog is None or not dialog.isVisible():
            return
        symbol = dialog.symbol
        profile_route = self._router.route(Capability.PROFILE, symbol)
        news_route = self._router.route(Capability.NEWS, symbol)
        if (profile_route, symbol) != self._profile_key:
            self._profile_key = (profile_route, symbol)
            dialog.load_profile(profile_route.spec.display_name)

            def load_profile(cancelled: Event) -> SymbolProfile | None:
                return None if cancelled.is_set() else profile_route.aggregator.fetch_profile(symbol)

            self._profile_job.submit(load_profile)

        if (news_route, symbol) != self._news_key:
            self._news_key = (news_route, symbol)
            dialog.load_news(news_route.spec.display_name)

            def load_news(cancelled: Event) -> tuple[NewsArticle, ...] | None:
                return None if cancelled.is_set() else news_route.aggregator.fetch_news(symbol)

            self._news_job.submit(load_news)

    def clear_company_name(self, symbol: str) -> None:
        if self.dialog is not None and self.dialog.isVisible() and self.dialog.symbol == symbol:
            self.dialog.set_company_name(None)

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
        self._profile_key = self._news_key = None
        self._profile_job.cancel()
        self._news_job.cancel()

    def _on_profile(self, profile: object = None) -> None:
        if self.dialog is not None and self.dialog.isVisible() and self._profile_key is not None:
            route, _ = self._profile_key
            self.dialog.set_profile(cast(SymbolProfile | None, profile), route.spec.display_name)

    def _on_news(self, articles: object = None) -> None:
        if self.dialog is not None and self.dialog.isVisible() and self._news_key is not None:
            route, _ = self._news_key
            self.dialog.set_news(cast(tuple[NewsArticle, ...] | None, articles), route.spec.display_name)

    def _on_idle(self) -> None:
        if not self.busy:
            self.idle.emit()
