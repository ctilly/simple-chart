# Simple Chart Help Guide

Simple Chart is designed to be simple and easy to use; no cruft or complicated UI.  It runs natively on Linux, and supported on Mac and Windows, fully extensible, customizable, and FREE.  
Extensible because it comes equipped with a robust Indicator and Tool API so you can build your own custom indicators and tools. The API is built specifically to faciliate agentic coding with your AI platform of choice. 
Customizable because the application is open source and you are free to change it to whatever suits your personal preference.
The app is free, source code and all.  It is licensed though make sure you comply with the license.  



## Chart Navigation

### Reset The View

Use `Ctrl+r` to reset the chart view.

This restores the default chart viewport when the chart has been panned,
zoomed, or vertically adjusted.

### Mouse Navigation

With a mouse:

- Drag left or right in the chart to move through time.
- Use the mouse wheel over the chart to expand or compact the visible time
  range.
- Drag the price axis on the right side of the price chart to adjust vertical
  perspective.

The mouse wheel is best for quick horizontal zooming. If the candles become too
flat or too tall, use the price axis rather than trying to fix the view with
more horizontal zoom.

### Trackpad Navigation

With a trackpad:

- Use a two-finger horizontal pan to move backward or forward through time.
- Pinch fingers together to compact the chart and show more candles.
- Spread fingers apart to expand the chart and show fewer candles in more
  detail.
- Keep horizontal gestures horizontal. Small diagonal movement can feel like a
  combined navigation gesture, especially during fast chart review.

Trackpad gestures are best for smooth navigation. Use deliberate, moderate
gestures rather than very fast pinches or flicks when you want precise control.

### Vertical Perspective

The horizontal axis controls time. The vertical axis controls price
perspective.

When you pan across time or change the number of visible candles, the visible
price range may no longer be ideal for the section you are studying. Candles can
look flattened when the vertical range is too wide, or cramped when the range is
too tight.

Use the right-side price axis to correct this:

- Drag on the price axis to make candles taller or shorter.
- Adjust the vertical perspective after moving to a different section of the
  chart.
- Use `Ctrl+r` when you want to return to the default view.

This is intentional: time navigation and price perspective are separate
controls. Horizontal gestures should answer "how much time do I want to see?"
The price axis should answer "how tall should price movement look?"

### Drawing And Interaction Shortcuts

- `Ctrl+r`: Reset Chart View
- `Esc`: Cancel Drawing

Use `Esc` when an in-progress drawing or drag operation should be abandoned.

## Timeframes

Simple Chart supports `5m`, `15m`, `30m`, `39m`, `65m`, daily, and weekly
views.

The `39m` and `65m` timeframes are built for swing trading workflows. They are
aggregated intraday views that divide the trading session into useful chunks
without forcing every review into standard calendar intervals.

## Indicators

Simple Chart indicators are designed to preserve higher-timeframe context while
you inspect lower-timeframe detail. Several indicators are intentionally
day-based or timestamp-based instead of simple "number of bars on the current
screen" calculations.

### Simple Moving Average

The Simple Moving Average uses trading days, not raw chart periods.

For example, a `50` SMA means a 50 trading-day average. On a daily chart that is
50 daily bars. On an intraday chart, Simple Chart converts 50 trading days into
the appropriate number of intraday bars.

This keeps the SMA anchored to the same market idea across timeframes. A
50-day SMA remains a 50-day SMA whether you are viewing daily candles or a 5m
chart.

### Exponential Moving Average

The Exponential Moving Average follows the same day-based convention as the
SMA.

An EMA gives more weight to recent price action, but its configured value still
means trading days. A `20` EMA is a 20 trading-day EMA across all timeframes.

### Pivot Points

Pivot Points are calculated from prior daily price action and then displayed on
the active chart.

This means the levels remain useful when switching into intraday timeframes:
you can inspect how price behaves around daily-derived support, resistance, and
pivot levels inside the session.

Available methods include standard, Fibonacci, and Camarilla pivots.

### Anchored VWAP

Anchored VWAP starts from a specific point in time that you choose on the chart.

The anchor is stored as a timestamp, not as "bar number 120" or another
timeframe-specific location. When you switch timeframes, the AVWAP resolves that
same timestamp onto the new chart.

This makes AVWAP useful for marking events such as earnings, news reactions,
breakouts, major lows, or major highs, then reviewing that same anchor across
daily and intraday views.

### RSI

RSI is displayed in its own panel below the price chart. It measures momentum on
a 0 to 100 scale, with reference levels for overbought and oversold conditions.

Like the moving averages, its configured length is day-based so the same
momentum idea carries across timeframes.

## Drawing Tools

Drawing tools are for visual markup while reviewing a symbol.

- Vertical Line marks a specific point in time.
- Fibonacci Retracement marks a move and shows retracement levels.

Use `Esc` to cancel an in-progress drawing.

## Practical Workflow

1. Start with daily or weekly to identify the larger structure.
2. Add the day-based indicators that define your review context.
3. Switch to an intraday timeframe to inspect the same levels in detail.
4. Use horizontal navigation to choose the amount of time visible.
5. Use the price axis to tune candle height.
6. Reset the view with `Ctrl+r` when the chart becomes hard to read.
