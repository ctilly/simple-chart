"""
chart/styles.py

Visual constants for SimpleChart.

All colors, line widths, and font sizes are defined here. Nothing in the
chart layer should hardcode a color string inline — reference a constant
from this module instead. That way the entire theme can be adjusted in
one place.

White background matched to Webull.
"""

from dataclasses import dataclass


# ------------------------------------------------------------------
# Background and grid
# ------------------------------------------------------------------

BACKGROUND       = "#ffffff"   # white (matched to Webull)
AXIS_TEXT_COLOR  = "#333333"   # dark text on white background


# ------------------------------------------------------------------
# Candles
# ------------------------------------------------------------------

CANDLE_UP        = "#00c853"   # true green
CANDLE_DOWN      = "#ff3d3d"   # red

# ------------------------------------------------------------------
# Volume bars — same hue as candles, semi-transparent so they
# don't overwhelm the price panel.  #RRGGBBAA format (pyqtgraph).
# ------------------------------------------------------------------

VOLUME_UP        = "#00c85366"   # CANDLE_UP  at ~40% opacity
VOLUME_DOWN      = "#ff3d3d66"   # CANDLE_DOWN at ~40% opacity


# ------------------------------------------------------------------
# Last-price line and its axis tag — kept lighter and smaller than the
# horizontal-line tool's tags.
# ------------------------------------------------------------------

LAST_PRICE_LINE_ALPHA = "80"   # 50% opacity
LAST_PRICE_TAG_ALPHA  = "b3"   # 70% opacity
LAST_PRICE_TAG_TEXT   = "#ffffff"


# ------------------------------------------------------------------
# Colors a theme supplies. The defaults are the light colors above; the
# app layer builds one from the active theme and passes it to the chart.
# ------------------------------------------------------------------

@dataclass(frozen=True)
class ChartColors:
    background:          str   = BACKGROUND
    foreground:          str   = AXIS_TEXT_COLOR
    odd_plot_background: str   = BACKGROUND
    hollow_body:         str   = BACKGROUND   # bull candle body
    grid_alpha:          float = 0.06         # grid lines use the axis color
    crosshair:           str   = "#00000077"
    candle_up:           str   = CANDLE_UP
    candle_down:         str   = CANDLE_DOWN
    volume_up:           str   = VOLUME_UP
    volume_down:         str   = VOLUME_DOWN
    legend_hidden:       str   = "#aaaaaa"    # a toggled-off legend label
    last_price:          str   = "#0000ff"    # last-price line and axis tag


# ------------------------------------------------------------------
# Line widths (pixels)
# ------------------------------------------------------------------

LINE_WIDTH_INDICATOR = 1.5   # MA lines, AVWAP


# ------------------------------------------------------------------
# Legend / overlay text
# ------------------------------------------------------------------

LEGEND_FONT_SIZE = 10   # points
