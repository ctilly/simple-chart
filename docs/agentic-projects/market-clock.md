# Market clock

The user approved Eastern Time on the lower left, shortcut hints in the center, and chart data source on the right. The clock displays New York local time as HH:MM ET year-round. Existing status messages replace the center hints for their normal lifetime. Shared footer typography uses the theme's foreground for Light and Dark contrast.

`MainWindow` installs `MarketStatusBar` at construction and stops its timer during shutdown. It retains normal `QStatusBar` message methods and renders those messages as plain text. No provider calls or dependencies are needed.

The production integration test failed at the missing clock, then verified alignment, winter and summer offsets, timer updates and shutdown, and temporary message visibility and expiry. The broader verification run passed 53 tests. Ruff and scoped mypy passed. Offscreen renders were inspected in Light and Dark. Claude design and implementation QC completed without outstanding issues. All verification and review processes exited.
