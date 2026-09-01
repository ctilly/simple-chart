from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
)

from app.dialogs import _FRAME_WIDTH
from app.extension_config import ExtensionConfigDialog
from simplechart.api import FloatParam


def test_session_lifecycle_group_disables_age_off_until_persistence_enabled(qtbot: Any) -> None:
    dialog = ExtensionConfigDialog(
        "Fibonacci Retracement",
        {
            "color": "#4f7cff",
            "persist_across_sessions": False,
            "age_off_days": FloatParam(2.0, minimum=0.0, maximum=3650.0, step=1.0, decimals=1),
            "show_price_labels": False,
        },
    )
    qtbot.addWidget(dialog)

    title_bar = dialog.findChild(QFrame, "dialogTitleBar")
    assert dialog.objectName() == "extensionConfigDialog"
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert f"border: {_FRAME_WIDTH}px solid" in dialog.styleSheet()
    assert title_bar is not None
    assert "border-bottom: 1px solid" in title_bar.styleSheet()

    group = _session_lifecycle_group(dialog)
    persist = group.findChild(QCheckBox)
    age_off = group.findChild(QDoubleSpinBox)

    assert persist is not None
    assert age_off is not None
    assert not persist.isChecked()
    assert not age_off.isEnabled()

    persist.setChecked(True)

    assert age_off.isEnabled()
    assert dialog.result_params()["persist_across_sessions"] is True
    assert dialog.result_params()["age_off_days"] == 2.0
    assert _last_form_widget(dialog) is group


def _session_lifecycle_group(dialog: ExtensionConfigDialog) -> QGroupBox:
    groups = [
        group for group in dialog.findChildren(QGroupBox)
        if group.title() == "Session Lifecycle"
    ]
    assert len(groups) == 1
    return groups[0]


def _last_form_widget(dialog: ExtensionConfigDialog) -> QGroupBox | None:
    forms = dialog.findChildren(QFormLayout)
    assert len(forms) >= 1
    form = forms[0]
    item = form.itemAt(form.rowCount() - 1, QFormLayout.ItemRole.SpanningRole)
    assert item is not None
    widget = item.widget()
    assert widget is None or isinstance(widget, QGroupBox)
    return widget
