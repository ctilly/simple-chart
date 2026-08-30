from datetime import UTC, datetime
from pathlib import Path

from data.cache import Cache


def test_asset_reference_is_shared_without_provider_identity(tmp_path: Path) -> None:
    refreshed_at = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_asset_reference(" spy ", "SPDR S&P 500 ETF Trust", refreshed_at)

        reference = cache.get_asset_reference("SPY")

    assert reference is not None
    assert reference.symbol == "SPY"
    assert reference.company_name == "SPDR S&P 500 ETF Trust"
    assert reference.refreshed_at == refreshed_at


def test_asset_reference_update_replaces_name_and_refresh_time(
    tmp_path: Path,
) -> None:
    first_refresh = datetime(2026, 7, 1, tzinfo=UTC)
    second_refresh = datetime(2026, 8, 1, tzinfo=UTC)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_asset_reference("META", "Facebook, Inc.", first_refresh)
        cache.put_asset_reference("META", "Meta Platforms, Inc.", second_refresh)

        reference = cache.get_asset_reference("meta")

    assert reference is not None
    assert reference.company_name == "Meta Platforms, Inc."
    assert reference.refreshed_at == second_refresh


def test_asset_reference_returns_none_for_unknown_symbol(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        assert cache.get_asset_reference("UNKNOWN") is None
