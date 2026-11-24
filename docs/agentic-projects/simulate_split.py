"""
Make a COPY of a SimpleChart database in which one symbol's cached bars look
as if they were cached before a 2-for-1 split, so the next chart load sees
the real provider data on a "new basis" and runs the Wave 3 flow.

    python simulate_split.py --source ~/.simplechart/simplechart.db \
        --dest ~/sc-split-test.db --symbol TSLA

Then run the app on the copy:  python main.py --db ~/sc-split-test.db

The source is opened read-only and never changed. For every cached
timeframe of the symbol (in each cache namespace, or only --namespace):
the newest cached session is deleted and the coverage end moved back to it,
so the next load refreshes the tail; every remaining bar and correction is
doubled in price and halved in volume (the old basis of a 2-for-1 split).
"""

import argparse
import sqlite3
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_REAL_DB = Path.home() / ".simplechart" / "simplechart.db"


def _day_start_ms(timestamp_ms: int) -> int:
    day = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone(ET).date()
    return int(datetime.combine(day, time.min, tzinfo=ET).timestamp() * 1000)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=_REAL_DB)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--namespace", default=None)
    args = parser.parse_args()
    dest = args.dest.expanduser().resolve()
    if dest == _REAL_DB.resolve():
        raise SystemExit("Refusing to modify the real database; choose another --dest.")
    if dest.exists():
        raise SystemExit(f"{dest} exists; remove it or choose another --dest.")
    symbol = args.symbol.strip().upper()

    source = sqlite3.connect(f"file:{args.source.expanduser()}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    source.backup(target)
    source.close()

    rows = target.execute(
        """
        SELECT cache_namespace, timeframe, MAX(timestamp)
        FROM bars WHERE symbol = ?
        GROUP BY cache_namespace, timeframe
        """,
        (symbol,),
    ).fetchall()
    rows = [row for row in rows if args.namespace in (None, row[0])]
    if not rows:
        raise SystemExit(f"No cached bars for {symbol}.")
    with target:
        for namespace, timeframe, newest_ms in rows:
            key = (namespace, symbol, timeframe)
            cut_ms = _day_start_ms(newest_ms)
            if timeframe == "weekly":
                cut_ms = newest_ms
            for table in ("bars", "bar_corrections"):
                target.execute(
                    f"DELETE FROM {table} WHERE cache_namespace = ? AND symbol = ?"
                    " AND timeframe = ? AND timestamp >= ?",
                    (*key, cut_ms),
                )
            target.execute(
                "UPDATE bar_fetch_coverage SET end_timestamp = MIN(end_timestamp, ?)"
                " WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?",
                (cut_ms, *key),
            )
            target.execute(
                "UPDATE bars SET open = open * 2, high = high * 2, low = low * 2,"
                " close = close * 2, vwap = vwap * 2,"
                " volume = CAST(ROUND(volume / 2.0) AS INTEGER)"
                " WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?",
                key,
            )
            target.execute(
                "UPDATE bar_corrections SET open = open * 2, high = high * 2,"
                " low = low * 2, close = close * 2,"
                " volume = CAST(ROUND(volume / 2.0) AS INTEGER)"
                " WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?",
                key,
            )
            cut = datetime.fromtimestamp(cut_ms / 1000, tz=timezone.utc).astimezone(ET)
            print(f"{namespace:22} {timeframe:7} old basis; cache now ends {cut:%Y-%m-%d %H:%M} ET")
    target.close()
    print(f"\nWrote {dest}. Run: python main.py --db {dest}")


if __name__ == "__main__":
    main()
