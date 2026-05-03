from pathlib import Path

from data.cache import Cache
from data.models import AnchorRecord


def test_update_anchor_persists_timestamp_and_display_options(tmp_path: Path) -> None:
    db_path = tmp_path / "simplechart.db"
    cache = Cache(str(db_path))
    try:
        anchor = cache.put_anchor(
            AnchorRecord(
                symbol="SPY",
                anchor_ts=1_700_000_000_000,
                label="2023-11-14",
                color="#9141ac",
            )
        )

        updated = AnchorRecord(
            symbol="SPY",
            anchor_ts=1_700_086_400_000,
            label="2023-11-15",
            color="#e01b24",
            line_width=2.5,
            line_style="dash",
            show_anchor=True,
            anchor_id=anchor.anchor_id,
        )
        cache.update_anchor(updated)

        anchors = cache.get_anchors("SPY")
    finally:
        cache.close()

    assert anchors == [updated]


def test_anchor_show_anchor_defaults_false(tmp_path: Path) -> None:
    db_path = tmp_path / "simplechart.db"
    cache = Cache(str(db_path))
    try:
        cache.put_anchor(
            AnchorRecord(
                symbol="QQQ",
                anchor_ts=1_700_000_000_000,
                label="2023-11-14",
                color="#9141ac",
            )
        )

        anchors = cache.get_anchors("QQQ")
    finally:
        cache.close()

    assert len(anchors) == 1
    assert anchors[0].show_anchor is False
    assert anchors[0].line_width == 2.0
