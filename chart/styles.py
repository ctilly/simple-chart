"""
chart/styles.py

Visual constants for SimpleChart.

All colors, line widths, and font sizes are defined here. Nothing in the
chart layer should hardcode a color string inline — reference a constant
from this module instead. That way the entire theme can be adjusted in
one place.

Dark background is the default — consistent with professional trading
platforms and easier on the eyes during long sessions.
"""


# ------------------------------------------------------------------
# Background and grid
# ------------------------------------------------------------------

BACKGROUND       = "#ffffff"   # white (matched to Webull)
GRID_COLOR       = "#eeeeee"   # very light gray grid lines
AXIS_COLOR       = "#cccccc"   # axis tick marks and border
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
# Default indicator colors
# A new indicator instance cycles through this list if the user has
# not explicitly chosen a color. The controller picks the next unused
# color from this palette when adding an indicator.
# ------------------------------------------------------------------

INDICATOR_PALETTE: list[str] = [
    "#00BFFF",   # deep sky blue   — SMA default
    "#FF8C00",   # dark orange     — EMA default
    "#00FF88",   # mint green      — AVWAP default
    "#FF69B4",   # hot pink
    "#FFD700",   # gold
    "#DA70D6",   # orchid
    "#7FFFD4",   # aquamarine
    "#FF6347",   # tomato
]


# ------------------------------------------------------------------
# Line widths (pixels)
# ------------------------------------------------------------------

LINE_WIDTH_INDICATOR  = 1.5   # MA lines, AVWAP
LINE_WIDTH_THIN       = 1.0   # secondary lines


# ------------------------------------------------------------------
# Crosshair
# ------------------------------------------------------------------

CROSSHAIR_COLOR = "#555555"
CROSSHAIR_WIDTH = 1


# ------------------------------------------------------------------
# Legend / overlay text
# ------------------------------------------------------------------

LEGEND_BACKGROUND   = "#1a1a1a"
LEGEND_TEXT_COLOR   = "#cccccc"
LEGEND_FONT_SIZE    = 10        # points
