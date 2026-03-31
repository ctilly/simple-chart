"""
plugins/builtin/__init__.py

Imports all built-in indicators so they self-register via the call to
register() at the bottom of each module. This file is imported once at
app startup — after this import, all built-ins are available in the
indicator registry.

To add a new built-in indicator:
  1. Create plugins/builtin/your_indicator.py
  2. Implement the Indicator ABC
  3. Call register(YourIndicator) at the bottom of the module
  4. Add the import here
"""

from plugins.builtin import avwap  # noqa: F401
from plugins.builtin import ema    # noqa: F401
from plugins.builtin import sma    # noqa: F401
