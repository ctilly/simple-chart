from typing import Any

from indicators.five_day_marker.models import FiveDayMarkerSettings
from simplechart.api import (
    ChartExtensionMutation,
    ChartExtensionStoreContext,
    ChartExtensionStoreRecord,
)

STORE_KEY = "five_day_marker.settings"
MARKER_KEY = "five_day_marker"
DEFAULT_COLOR = "#800080"
DEFAULT_LINE_WIDTH = 1.0
DEFAULT_LINE_STYLE = "dash"
DEFAULT_VISIBLE = True

_SORT_KEY = 0


class FiveDayMarkerStore:

    def __init__(self, context: ChartExtensionStoreContext) -> None:
        self._context = context
        self._settings: FiveDayMarkerSettings | None = None

    def load_for_symbol(self, symbol: str) -> None:
        records = self._context.get_extension_records(STORE_KEY, symbol)
        if records:
            self._settings = _record_to_settings(records[0])
            return

        record = self._context.put_extension_record(
            STORE_KEY,
            symbol,
            _SORT_KEY,
            _settings_payload(_default_settings(symbol)),
        )
        self._settings = _record_to_settings(record)

    def settings(self) -> FiveDayMarkerSettings | None:
        return self._settings

    def apply(self, mutation: ChartExtensionMutation) -> None:
        if mutation.extension_name != MARKER_KEY:
            return
        if mutation.operation == "update_settings":
            self._update_settings(mutation.payload)
        elif mutation.operation == "disable":
            self._disable()

    def prepare_active_extensions(self) -> None:
        if self._settings is None:
            return
        if self._settings.enabled:
            self._context.ensure_extension_state(MARKER_KEY, _params_from_settings(self._settings))
        else:
            self._context.remove_extension_state(MARKER_KEY)

    def params_for(
        self,
        extension_name: str,
        base_params: dict[str, Any],
    ) -> dict[str, Any]:
        if extension_name != MARKER_KEY or self._settings is None:
            return base_params
        params = dict(base_params)
        params.update(_params_from_settings(self._settings))
        return params

    def _update_settings(self, payload: dict[str, Any]) -> None:
        symbol = self._context.current_symbol()
        if symbol is None:
            return
        current = self._settings or _default_settings(symbol)
        updated = FiveDayMarkerSettings(
            symbol=symbol,
            enabled=True,
            color=str(payload["color"]),
            line_width=float(payload["line_width"]),
            line_style=str(payload["line_style"]),
            visible=bool(payload["visible"]),
            record_id=current.record_id,
        )
        self._settings = self._persist(updated)
        self.prepare_active_extensions()

    def _disable(self) -> None:
        if self._settings is None:
            return
        updated = FiveDayMarkerSettings(
            symbol=self._settings.symbol,
            enabled=False,
            color=self._settings.color,
            line_width=self._settings.line_width,
            line_style=self._settings.line_style,
            visible=False,
            record_id=self._settings.record_id,
        )
        self._settings = self._persist(updated)
        self._context.remove_series_visibility(MARKER_KEY, MARKER_KEY)
        self._context.remove_extension_state(MARKER_KEY)

    def _persist(self, settings: FiveDayMarkerSettings) -> FiveDayMarkerSettings:
        payload = _settings_payload(settings)
        if settings.record_id is None:
            record = self._context.put_extension_record(
                STORE_KEY,
                settings.symbol,
                _SORT_KEY,
                payload,
            )
            return _record_to_settings(record)
        self._context.update_extension_record(settings.record_id, _SORT_KEY, payload)
        return settings


def _default_settings(symbol: str) -> FiveDayMarkerSettings:
    return FiveDayMarkerSettings(
        symbol=symbol,
        enabled=True,
        color=DEFAULT_COLOR,
        line_width=DEFAULT_LINE_WIDTH,
        line_style=DEFAULT_LINE_STYLE,
        visible=DEFAULT_VISIBLE,
    )


def _params_from_settings(settings: FiveDayMarkerSettings) -> dict[str, Any]:
    return {
        "enabled": settings.enabled,
        "color": settings.color,
        "line_width": settings.line_width,
        "line_style": settings.line_style,
        "visible": settings.visible,
    }


def _settings_payload(settings: FiveDayMarkerSettings) -> dict[str, Any]:
    return {
        "enabled": settings.enabled,
        "color": settings.color,
        "line_width": settings.line_width,
        "line_style": settings.line_style,
        "visible": settings.visible,
    }


def _record_to_settings(record: ChartExtensionStoreRecord) -> FiveDayMarkerSettings:
    payload = record.payload
    return FiveDayMarkerSettings(
        symbol=record.symbol,
        enabled=bool(payload["enabled"]),
        color=str(payload["color"]),
        line_width=float(payload["line_width"]),
        line_style=str(payload["line_style"]),
        visible=bool(payload["visible"]),
        record_id=record.record_id,
    )
