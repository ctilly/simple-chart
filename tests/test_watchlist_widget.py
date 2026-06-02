from PyQt6.QtWidgets import QLabel

from app.watchlist import SymbolListWidget, SymbolTileWidget, WatchlistWidget


def test_symbol_list_manages_flat_symbol_order(qtbot) -> None:  # type: ignore[no-untyped-def]
    symbol_list = SymbolListWidget(["SPY", "QQQ"])
    qtbot.addWidget(symbol_list)

    symbol_list.add_symbol("IWM")
    symbol_list.add_symbol("QQQ")
    symbol_list.remove_symbol("SPY")

    assert symbol_list.symbols() == ["QQQ", "IWM"]


def test_symbol_list_sets_active_symbol(qtbot) -> None:  # type: ignore[no-untyped-def]
    symbol_list = SymbolListWidget(["SPY", "QQQ", "IWM"])
    qtbot.addWidget(symbol_list)

    symbol_list.set_active_symbol("QQQ")

    current = symbol_list.currentItem()
    assert current is not None
    assert symbol_list.symbols()[symbol_list.currentRow()] == "QQQ"


def test_watchlist_widget_routes_reorder_callback(qtbot) -> None:  # type: ignore[no-untyped-def]
    reordered: list[list[str]] = []
    watchlist = WatchlistWidget(
        symbols=["SPY", "QQQ"],
        on_add=lambda symbol: None,
        on_remove=lambda symbol: None,
        on_reorder=reordered.append,
    )
    qtbot.addWidget(watchlist)

    symbol_list = watchlist.findChild(SymbolListWidget)
    assert symbol_list is not None
    symbol_list.order_changed.emit(["QQQ", "SPY"])

    assert reordered == [["QQQ", "SPY"]]


def test_symbol_list_displays_percent_changes(qtbot) -> None:  # type: ignore[no-untyped-def]
    symbol_list = SymbolListWidget(["SPY", "QQQ", "IWM"])
    qtbot.addWidget(symbol_list)

    symbol_list.set_percent_changes({"SPY": 1.234, "QQQ": -0.456, "IWM": 0.0})

    spy_tile = symbol_list.tile_for_symbol("SPY")
    qqq_tile = symbol_list.tile_for_symbol("QQQ")
    iwm_tile = symbol_list.tile_for_symbol("IWM")
    assert spy_tile is not None
    assert qqq_tile is not None
    assert iwm_tile is not None
    assert spy_tile.percent_text() == "+1.23%"
    assert qqq_tile.percent_text() == "-0.46%"
    assert iwm_tile.percent_text() == "0.00%"

    spy_label = spy_tile.findChild(QLabel, "watchlistPercentLabel")
    qqq_label = qqq_tile.findChild(QLabel, "watchlistPercentLabel")
    assert spy_label is not None
    assert qqq_label is not None
    assert "#1f8f45" in spy_label.styleSheet()
    assert "#c7392f" in qqq_label.styleSheet()


def test_symbol_tile_displays_unknown_percent_as_placeholder(qtbot) -> None:  # type: ignore[no-untyped-def]
    tile = SymbolTileWidget("SPY")
    qtbot.addWidget(tile)

    tile.set_percent_change(None)

    label = tile.findChild(QLabel, "watchlistPercentLabel")
    assert label is not None
    assert tile.percent_text() == "--"
    assert "#888888" in label.styleSheet()
