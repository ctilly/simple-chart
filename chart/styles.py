"""
chart/styles.py

Visual constants for SimpleChart.

All colors, line widths, and font sizes are defined here. Nothing in the
chart layer should hardcode a color string inline — reference a constant
from this module instead. That way the entire theme can be adjusted in
one place.

White background matched to Webull.
"""


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
# AVWAP colors
# Assigned in order; cycles back to teal after the first two.
# ------------------------------------------------------------------

AVWAP_PALETTE: list[str] = [
    "#9141ac",   # purple — first AVWAP
    "#e01b24",   # red    — second AVWAP
    "#2190a4",   # teal   — third and beyond (cycles)
]


# ------------------------------------------------------------------
# Line widths (pixels)
# ------------------------------------------------------------------

LINE_WIDTH_INDICATOR = 1.5   # MA lines, AVWAP


# ------------------------------------------------------------------
# Legend / overlay text
# ------------------------------------------------------------------

LEGEND_FONT_SIZE = 10   # points
