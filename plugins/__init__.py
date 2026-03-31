"""
plugins/__init__.py

Top-level plugin loader. Imports all indicator plugins so their
register() calls fire at app startup.

Built-in indicators (shipped with the app):
    Imported via plugins/builtin/__init__.py — add new built-ins there.

Example / contributed indicators:
    Imported directly here. Add a line for each plugin file that lives
    in the plugins/ directory (i.e. not inside builtin/).
"""

from plugins import builtin          # noqa: F401 — registers all built-ins
from plugins import example_plugin   # noqa: F401 — RSI example; register or remove
