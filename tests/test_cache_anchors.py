from pathlib import Path

from data.cache import Cache
from data.models import ChartExtensionStoreRecord


def test_update_indicator_record_persists_sort_key_and_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "simplechart.db"
    cache = Cache(str(db_path))
    try:
        record = cache.put_indicator_record(
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
        cache.update_indicator_record(
            updated.record_id,
            updated.sort_key,
            updated.payload,
        )

        records = cache.get_indicator_records("avwap.anchors", "SPY")
    finally:
        cache.close()

    assert records == [updated]


def test_indicator_records_are_scoped_by_store_key_and_symbol(tmp_path: Path) -> None:
    db_path = tmp_path / "simplechart.db"
    cache = Cache(str(db_path))
    try:
        cache.put_indicator_record(
            "avwap.anchors",
            "QQQ",
            1_700_000_000_000,
            {"label": "2023-11-14"},
        )
        cache.put_indicator_record(
            "fib.retracements",
            "QQQ",
            1_700_000_000_000,
            {"label": "2023-11-14"},
        )

        records = cache.get_indicator_records("avwap.anchors", "QQQ")
    finally:
        cache.close()

    assert len(records) == 1
    assert records[0].store_key == "avwap.anchors"
