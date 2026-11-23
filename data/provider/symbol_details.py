import logging
import math
from dataclasses import dataclass
from datetime import datetime
from numbers import Real
from urllib.parse import urlsplit

import yfinance as yf

from data.errors import describe


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    weight: float


@dataclass(frozen=True)
class SymbolProfile:
    name: str | None = None
    description: str | None = None
    float_shares: int | None = None
    is_etf: bool = False
    issuer: str | None = None
    benchmark: str | None = None
    management_fee: float | None = None
    expense_ratio: float | None = None
    holdings: tuple[Holding, ...] = ()


@dataclass(frozen=True)
class NewsArticle:
    headline: str
    publisher: str | None
    published: datetime | None
    url: str | None


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def fetch_profile(symbol: str) -> SymbolProfile | None:
    ticker = yf.Ticker(symbol)
    try:
        info = ticker.get_info()
    except Exception as exc:
        _LOGGER.debug("Yahoo profile request failed: %s", describe(exc))
        return None
    if not isinstance(info, dict) or not info:
        return None
    name = _text(info.get("longName")) or _text(info.get("shortName"))
    description = _text(info.get("longBusinessSummary"))
    if info.get("quoteType") != "ETF":
        shares = _number(info.get("floatShares"))
        return SymbolProfile(name, description, int(shares) if shares is not None else None)

    issuer = _text(info.get("fundFamily"))
    benchmark = _text(info.get("benchmark"))
    expense_ratio = None
    holdings: list[Holding] = []
    try:
        fund = ticker.funds_data
        description = _text(fund.description) or description
        issuer = _text(fund.fund_overview.get("family")) or issuer
        operations = fund.fund_operations
        if "Annual Report Expense Ratio" in operations.index and symbol in operations:
            expense_ratio = _number(operations.at["Annual Report Expense Ratio", symbol])
        for holding_symbol, row in fund.top_holdings.iterrows():
            ticker_name = _text(holding_symbol)
            weight = _number(row.get("Holding Percent"))
            if ticker_name is not None and weight is not None:
                holdings.append(Holding(ticker_name, _text(row.get("Name")) or ticker_name, weight))
    except Exception as exc:
        _LOGGER.debug("Yahoo fund profile request failed: %s", describe(exc))

    if symbol == "QQQ":
        benchmark = benchmark or "Nasdaq-100"
    return SymbolProfile(
        name=name, description=description, is_etf=True, issuer=issuer,
        benchmark=benchmark, management_fee=_number(info.get("managementFee")),
        expense_ratio=expense_ratio,
        holdings=tuple(sorted(holdings, key=lambda holding: holding.weight, reverse=True)[:20]),
    )


def _web_url(value: object) -> str | None:
    url = _text(value)
    if url is None:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
            return url
    except ValueError:
        pass
    return None


def _published(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is not None:
                return moment
        except ValueError:
            pass
    return None


def fetch_news(symbol: str) -> tuple[NewsArticle, ...] | None:
    try:
        stories = yf.Ticker(symbol).get_news(count=10)
    except Exception as exc:
        _LOGGER.debug("Yahoo news request failed: %s", describe(exc))
        return None
    if not isinstance(stories, list):
        return None
    articles: list[NewsArticle] = []
    for story in stories:
        content = story.get("content") if isinstance(story, dict) else None
        if not isinstance(content, dict) or (headline := _text(content.get("title"))) is None:
            continue
        provider = content.get("provider")
        publisher = _text(provider.get("displayName")) if isinstance(provider, dict) else None
        url = None
        for key in ("canonicalUrl", "clickThroughUrl"):
            link = content.get(key)
            url = _web_url(link.get("url")) if isinstance(link, dict) else None
            if url is not None:
                break
        articles.append(NewsArticle(headline, publisher, _published(content.get("pubDate")), url))
    articles.sort(key=lambda item: item.published.timestamp() if item.published else float("-inf"), reverse=True)
    return tuple(articles[:10])
