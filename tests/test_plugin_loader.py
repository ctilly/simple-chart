from pathlib import Path
from textwrap import dedent

from simplechart.api import all_extensions
from simplechart.plugins import load_plugin_directory


def test_load_plugin_directory_registers_indicator(tmp_path: Path) -> None:
    plugin_file = tmp_path / "custom_indicator.py"
    plugin_file.write_text(
        dedent(
            """
            from typing import Any

            import numpy as np

            from simplechart.api import ChartExtension, OHLCVSeries, register_extension


            class CustomPluginIndicator(ChartExtension):

                def name(self) -> str:
                    return "custom_plugin_indicator"

                def label(self) -> str:
                    return "Custom Plugin ChartExtension"

                def default_params(self) -> dict[str, Any]:
                    return {"color": "#ffffff"}

                def compute(
                    self,
                    series: OHLCVSeries,
                    params: dict[str, Any],
                ) -> dict[str, np.ndarray]:
                    return {"custom_plugin_indicator": np.full(len(series.bars), np.nan)}


            register_extension(CustomPluginIndicator)
            """
        )
    )

    load_plugin_directory(tmp_path)

    indicator = all_extensions()["custom_plugin_indicator"]()
    assert indicator.label() == "Custom Plugin ChartExtension"
