from datetime import UTC, datetime, time, timedelta

from data.models import Bar, SymbolProfile, Timeframe
from data.provider.base import DataProvider
from data.provider.credentials import ProviderCredentials


TEST_TOKEN = "SYNTHf4k7Q2yF3"
FAILURE_SENTINEL = "SYNTHv9X8p6R2q"
PROFILE_DESCRIPTION = "A profile supplied by the fixture source."
BAR_CLOSE = 246.0


class FakeSource(DataProvider):
    def __init__(self, credentials: ProviderCredentials, tier: str) -> None:
        self.credentials = credentials
        self.tier = tier

    def check_connection(self) -> None:
        token = self.credentials.get("token")
        if token != TEST_TOKEN:
            # Simulate an SDK that leaks unlabelled input in its exception.
            raise RuntimeError(token)

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.DAILY]

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        bars = []
        stamp = datetime.combine(start.date(), time.min, UTC)
        while stamp <= end:
            if stamp >= start and stamp.weekday() < 5:
                bars.append(Bar(stamp, BAR_CLOSE, BAR_CLOSE + 1, BAR_CLOSE - 1, BAR_CLOSE, 100))
            stamp += timedelta(days=1)
        return bars

    def fetch_profile(self, symbol: str) -> SymbolProfile:
        return SymbolProfile(name=f"{symbol} Fixture", description=PROFILE_DESCRIPTION)
