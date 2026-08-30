from pathlib import Path
from typing import Any

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QDialogButtonBox, QFrame, QLabel, QWidget


_APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def test_production_app_does_not_use_native_message_boxes() -> None:
    for relative_path in ("controller.py", "application_settings.py"):
        source = (_APP_ROOT / relative_path).read_text()
        assert "QMessageBox" not in source


def test_application_message_dialog_uses_shared_shell(qtbot: Any) -> None:
    from app.dialogs import ApplicationMessageDialog, MessageKind

    dialog = ApplicationMessageDialog(
        "Load Error",
        "The provider rejected this request.",
        MessageKind.WARNING,
    )
    qtbot.addWidget(dialog)

    title_bar = dialog.findChild(QFrame, "dialogTitleBar")
    message = dialog.findChild(QLabel, "applicationMessageText")
    icon = dialog.findChild(QLabel, "applicationMessageIcon")
    buttons = dialog.findChild(QDialogButtonBox, "applicationMessageButtons")

    assert dialog.objectName() == "applicationMessageDialog"
    assert "border: 2px solid" in dialog.styleSheet()
    assert title_bar is not None
    assert message is not None
    assert message.text() == "The provider rejected this request."
    assert message.wordWrap()
    assert icon is not None
    assert icon.pixmap() is not None
    assert not icon.pixmap().isNull()
    assert buttons is not None


def test_dialog_shell_derives_colors_from_inherited_palette(qtbot: Any) -> None:
    from app.dialogs import ApplicationMessageDialog, MessageKind

    parent = QWidget()
    palette = parent.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#202124"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f1f3f4"))
    parent.setPalette(palette)
    qtbot.addWidget(parent)

    dialog = ApplicationMessageDialog(
        "Dark Palette",
        "Palette-derived shell",
        MessageKind.INFORMATION,
        parent,
    )
    qtbot.addWidget(dialog)

    assert "#202124" in dialog.styleSheet()
    title_bar = dialog.findChild(QFrame, "dialogTitleBar")
    message = dialog.findChild(QLabel, "applicationMessageText")
    assert title_bar is not None
    assert "#f1f3f4" in title_bar.styleSheet()
    assert message is not None
    assert "QDialog#applicationMessageDialog QLabel" in dialog.styleSheet()
