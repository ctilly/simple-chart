from pathlib import Path

from data.cache import Cache
from data.models import ChartExtensionStoreRecord
from provider_support import startup_registry


def test_update_extension_record_persists_sort_key_and_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "simplechart.db"
    cache = Cache(str(db_path), registry=startup_registry())
    try:
        record = cache.put_extension_record(
            "avwap.anchors",
            "SPY",
            1_700_000_000_000,
            {
                "label": "2023-11-14",
                "color": "#9141ac",
                "line_width": 2.0,
                "line_style": "solid",
                "show_anchor": False,
            },
        )

        updated = ChartExtensionStoreRecord(
            record_id=record.record_id,
            store_key="avwap.anchors",
            symbol="SPY",
            sort_key=1_700_086_400_000,
            payload={
                "label": "2023-11-15",
                "color": "#e01b24",
                "line_width": 2.5,
                "line_style": "dash",
                "show_anchor": True,
            },
        )
        cache.update_extension_record(
            updated.record_id,
            updated.sort_key,
            updated.payload,
        )

    finally:
        cache.close()

    with Cache(str(db_path), registry=startup_registry()) as reopened:
        records = reopened.get_extension_records("avwap.anchors", "SPY")

    assert records == [updated]


def test_extension_records_are_scoped_by_store_key_and_symbol(tmp_path: Path) -> None:
    db_path = tmp_path / "simplechart.db"
    cache = Cache(str(db_path), registry=startup_registry())
    try:
        expected = cache.put_extension_record(
            "avwap.anchors",
            "QQQ",
            1_700_000_000_000,
            {"label": "2023-11-14"},
        )
        cache.put_extension_record(
            "fib.retracements",
            "QQQ",
            1_700_000_000_000,
            {"label": "2023-11-14"},
        )
        cache.put_extension_record(
            "avwap.anchors",
            "SPY",
            1_700_000_000_000,
            {"label": "competing symbol"},
        )

        records = cache.get_extension_records("avwap.anchors", "QQQ")
    finally:
        cache.close()

    assert records == [expected]
