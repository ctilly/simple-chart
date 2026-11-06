from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
import yfinance as yf
from yfinance.data import YfData
from yfinance.exceptions import YFPricesMissingError

from app.controller import _ChartLoadRequest, _FetchWorker, _fetch_and_cache_bars
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, Timeframe
from data.provider.base import HistoryUnavailableError
from data.provider.config import yfinance_connection
from data.provider.credentials import UnavailableCredentialStore
from data.provider.yfinance_provider import YFinanceProvider, _fetch_fast_info_snapshot


class _TimezoneCache:
    def lookup(self, symbol: str) -> str:
        return "America/New_York"

    def store(self, symbol: str, timezone: str | None) -> None:
        pass


class _Response:
    text = "synthetic Yahoo response"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _YahooTransport:
    def __init__(self) -> None:
        self.fail_history = False
        self.calls: list[str] = []

    def request(self, url: str) -> _Response:
        self.calls.append(url)
        symbol = url.rsplit("/", 1)[-1]
        if self.fail_history or symbol == "BROKEN" or "/chart/" not in url:
            raise RuntimeError("synthetic Yahoo transport failure")
        if symbol == "MISSING":
            return _Response({"chart": {"result": None, "error": {
                "description": "synthetic missing prices",
            }}})
        timestamps = [
            int(datetime(2026, 10, day, 13, 30, tzinfo=UTC).timestamp())
            for day in (27, 28)
        ]
        payload: dict[str, Any] = {"chart": {"error": None, "result": [{
            "meta": {
                "instrumentType": "EQUITY",
                "exchangeTimezoneName": "America/New_York",
                "currency": "USD",
                "validRanges": ["1d", "5d", "1y"],
            },
            "timestamp": timestamps,
            "indicators": {
                "quote": [{
                    "open": [100.0, 110.0], "high": [101.0, 111.0],
                    "low": [99.0, 109.0], "close": [100.0, 110.0],
                    "volume": [1000, 1100],
                }],
                "adjclose": [{"adjclose": [100.0, 110.0]}],
            },
        }]}}
        if symbol == "EMPTY":
            result = payload["chart"]["result"][0]
            result["timestamp"] = []
            result["indicators"] = {"quote": [{
                "open": [], "high": [], "low": [], "close": [], "volume": [],
            }]}
        return _Response(payload)


@pytest.fixture
def yahoo_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[_YahooTransport]:
    transport = _YahooTransport()
    debug = dict(yf.config.debug.data)
    network = dict(yf.config.network.data)

    def get(
        data: YfData,
        url: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> _Response:
        return transport.request(url)

    # Keep the real Ticker/history/download paths; replace data I/O and the
    # timezone cache so these tests never access Yahoo or the user's cache.
    monkeypatch.setattr(YfData, "get", get)
    monkeypatch.setattr(YfData, "cache_get", get)
    monkeypatch.setattr("yfinance.cache.get_tz_cache", _TimezoneCache)
    for name in ("_DFS", "_ERRORS", "_TRACEBACKS", "_ISINS"):
        monkeypatch.setattr(yf.shared, name, {})
    try:
        yield transport
    finally:
        yf.config.debug.data.clear()
        yf.config.debug.data.update(debug)
        yf.config.network.data.clear()
        yf.config.network.data.update(network)


def test_real_yahoo_history_propagates_transport_failure(
    yahoo_transport: _YahooTransport,
) -> None:
    yahoo_transport.fail_history = True
    end = datetime(2026, 10, 29, 18, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="synthetic Yahoo transport failure"):
        YFinanceProvider().fetch_bars(
            "SPY", Timeframe.MIN5, end - timedelta(days=1), end,
        )
    assert yahoo_transport.calls


def test_real_yahoo_missing_prices_and_disabled_config_control(
    yahoo_transport: _YahooTransport,
) -> None:
    end = datetime(2026, 10, 29, 18, tzinfo=UTC)
    provider = YFinanceProvider()
    with pytest.raises(HistoryUnavailableError):
        provider.fetch_bars("MISSING", Timeframe.MIN5, end - timedelta(days=1), end)

    yf.config.debug.hide_exceptions = True
    assert yf.Ticker("MISSING").history(
        start=end - timedelta(days=1), end=end, interval="5m",
    ).empty
    assert provider.fetch_bars(
        "MISSING", Timeframe.MIN5, end - timedelta(days=1), end,
    ) == []


def test_yahoo_initialization_exposes_real_history_errors_without_legacy_flag(
    yahoo_transport: _YahooTransport,
) -> None:
    YFinanceProvider()
    end = datetime(2026, 10, 29, 18, tzinfo=UTC)
    with pytest.raises(YFPricesMissingError):
        yf.Ticker("MISSING").history(
            start=end - timedelta(days=1), end=end, interval="5m",
        )


@pytest.mark.parametrize(
    "coverage_case", ["cold", "head", "replace", "tail", "bars_without_coverage"],
)
def test_real_yahoo_transport_failure_preserves_cache_coverage(
    yahoo_transport: _YahooTransport,
    tmp_path: Path,
    coverage_case: str,
) -> None:
    yahoo_transport.fail_history = True
    start = datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
    end = start + timedelta(hours=2)
    existing = Bar(start, 100.0, 101.0, 99.0, 100.5, 1000)
    namespace = yfinance_connection().cache_namespace
    coverage = None
    if coverage_case == "head":
        coverage = (int((start + timedelta(hours=1)).timestamp() * 1000),
                    int(end.timestamp() * 1000))
    elif coverage_case == "replace":
        coverage = (int((start - timedelta(days=2)).timestamp() * 1000),
                    int((start - timedelta(days=1)).timestamp() * 1000))
    elif coverage_case == "tail":
        coverage = (int(start.timestamp() * 1000),
                    int((start + timedelta(hours=1)).timestamp() * 1000))
    with Cache(str(tmp_path / "test.db")) as cache:
        has_bars = coverage is not None or coverage_case == "bars_without_coverage"
        if has_bars:
            cache.put_bars(namespace, "SPY", Timeframe.MIN5, [existing])
        if coverage is not None:
            cache.extend_bar_fetch_coverage(namespace, "SPY", Timeframe.MIN5, *coverage)
        with pytest.raises(RuntimeError, match="synthetic Yahoo transport failure"):
            _fetch_and_cache_bars(
                Aggregator(YFinanceProvider()), cache, namespace,
                "SPY", Timeframe.MIN5, start, end,
            )
        assert cache.get_bar_fetch_coverage(namespace, "SPY", Timeframe.MIN5) == coverage
        assert cache.get_bars(
            namespace, "SPY", Timeframe.MIN5,
            int(start.timestamp() * 1000), int(end.timestamp() * 1000),
        ) == (
            [existing] if has_bars else []
        )


def test_fetch_worker_real_yahoo_failure_emits_error_without_coverage(
    yahoo_transport: _YahooTransport,
    tmp_path: Path,
) -> None:
    yahoo_transport.fail_history = True
    db_path = str(tmp_path / "test.db")
    request = _ChartLoadRequest(1, "SPY", Timeframe.MIN5, yfinance_connection())
    worker = _FetchWorker(db_path, UnavailableCredentialStore("not needed"), request)
    errors: list[str] = []
    successes: list[object] = []
    worker.error.connect(lambda identity, message: errors.append(message))
    worker.finished.connect(lambda identity, series: successes.append(series))
    worker.run()
    assert errors == ["synthetic Yahoo transport failure"]
    assert successes == []
    with Cache(db_path) as cache:
        for timeframe in (Timeframe.DAILY, Timeframe.MIN5):
            assert cache.get_bar_fetch_coverage(
                request.cache_namespace, "SPY", timeframe,
            ) is None


def test_real_yahoo_mixed_batch_preserves_valid_symbol_and_limits_fallback(
    yahoo_transport: _YahooTransport,
) -> None:
    snapshots = YFinanceProvider().fetch_snapshots(["SPY", "BROKEN"])
    assert set(snapshots) == {"SPY"}
    assert snapshots["SPY"].change_percent == pytest.approx(10.0)
    spy_requests = [url for url in yahoo_transport.calls if url.endswith("/SPY")]
    assert len(spy_requests) == 1
    assert any(url.endswith("/BROKEN") for url in yahoo_transport.calls)


def test_real_yahoo_level1_failure_remains_best_effort(
    yahoo_transport: _YahooTransport,
) -> None:
    assert YFinanceProvider().fetch_level1("SPY") is None
    assert yahoo_transport.calls


@pytest.mark.parametrize("symbol", ["EMPTY", "MISSING"])
@pytest.mark.parametrize("cached_tail", [False, True])
def test_real_yahoo_empty_and_missing_prices_preserve_cache_policy(
    yahoo_transport: _YahooTransport,
    tmp_path: Path,
    symbol: str,
    cached_tail: bool,
) -> None:
    start = datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
    end = start + timedelta(hours=2)
    initial_coverage = (
        int(start.timestamp() * 1000),
        int((start + timedelta(hours=1)).timestamp() * 1000),
    )
    namespace = yfinance_connection().cache_namespace
    bar = Bar(start, 100.0, 101.0, 99.0, 100.5, 1000)
    with Cache(str(tmp_path / "test.db")) as cache:
        if cached_tail:
            cache.put_bars(namespace, symbol, Timeframe.MIN5, [bar])
            cache.extend_bar_fetch_coverage(
                namespace, symbol, Timeframe.MIN5, *initial_coverage,
            )
        aggregator = Aggregator(YFinanceProvider())
        if symbol == "MISSING" and not cached_tail:
            with pytest.raises(HistoryUnavailableError):
                _fetch_and_cache_bars(
                    aggregator, cache, namespace, symbol, Timeframe.MIN5, start, end,
                )
            expected_coverage = None
        else:
            _fetch_and_cache_bars(
                aggregator, cache, namespace, symbol, Timeframe.MIN5, start, end,
            )
            expected_coverage = initial_coverage if cached_tail else (
                int(start.timestamp() * 1000), int(end.timestamp() * 1000),
            )
        assert cache.get_bar_fetch_coverage(
            namespace, symbol, Timeframe.MIN5,
        ) == expected_coverage
        assert cache.get_bars(
            namespace, symbol, Timeframe.MIN5,
            int(start.timestamp() * 1000), int(end.timestamp() * 1000),
        ) == ([bar] if cached_tail else [])
    assert yahoo_transport.calls


@pytest.mark.parametrize("hide_exceptions", [True, False])
def test_real_yahoo_degraded_quotes_retain_history_fallback(
    yahoo_transport: _YahooTransport,
    hide_exceptions: bool,
) -> None:
    yf.config.debug.hide_exceptions = hide_exceptions
    snapshot = _fetch_fast_info_snapshot("SPY")
    assert snapshot is not None
    assert snapshot.last_price == pytest.approx(110.0)
    assert snapshot.change_percent == pytest.approx(10.0)
    assert YFinanceProvider().fetch_level1("SPY") is None
    assert any("/chart/SPY" in url for url in yahoo_transport.calls)
    assert any("/chart/" not in url for url in yahoo_transport.calls)


def test_yahoo_factory_configures_exceptions_once_in_fresh_process() -> None:
    script = '''
import sys
sys.path.insert(0, sys.argv[1])
import yfinance as yf
yf.config.debug.hide_exceptions = True
from data.provider import create_provider
from data.provider.config import yfinance_connection
from data.provider.credentials import UnavailableCredentialStore
assert "data.provider.yfinance_provider" not in sys.modules
assert yf.config.debug.hide_exceptions is True
store = UnavailableCredentialStore("not needed")
create_provider(yfinance_connection(), store)
assert yf.config.debug.hide_exceptions is False
yf.config.debug.hide_exceptions = True
create_provider(yfinance_connection(), store)
assert yf.config.debug.hide_exceptions is True
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1])],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
