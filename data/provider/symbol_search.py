import logging
from dataclasses import dataclass

import yfinance as yf

from data.errors import describe


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SymbolMatch:
    symbol: str
    name: str
    exchange: str


def search_symbols(query: str) -> tuple[SymbolMatch, ...]:
    try:
        quotes = yf.Search(
            query, max_results=8, news_count=0, lists_count=0,
            include_cb=False, recommended=0, timeout=5,
        ).quotes
    except Exception as exc:
        _LOGGER.debug("Yahoo symbol search failed: %s", describe(exc))
        return ()

    matches: dict[str, SymbolMatch] = {}
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        symbol = quote.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        name = quote.get("longname") or quote.get("shortname")
        exchange = quote.get("exchDisp") or quote.get("exchange")
        matches.setdefault(symbol, SymbolMatch(
            symbol,
            name if isinstance(name, str) else "",
            exchange if isinstance(exchange, str) else "",
        ))
    return tuple(matches.values())
