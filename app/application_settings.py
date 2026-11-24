import logging
from collections.abc import Callable, Mapping, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.dialogs import build_dialog_content, show_information, show_warning
from app.errors import ErrorContext, report_error
from app.data_quality import (
    BarComparisonServiceLike,
    DataQualityTab,
    SymbolRefresher,
)
from data.adjustment import BasisChangeChoice
from data.cache import Cache
from data.models import Timeframe
from data.provider import ProviderAvailability
from data.provider.config import ProviderConnection
from data.provider.credentials import CredentialStore, ProviderCredentials


_LOGGER = logging.getLogger(__name__)


class ConnectionDialog(QDialog):
    def __init__(
        self,
        connection: ProviderConnection,
        cache: Cache,
        credential_store: CredentialStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._connection = connection
        self._cache = cache
        self._credential_store = credential_store
        spec, _ = cache.registry.connection_template(connection.connection_id)
        self._has_stored_credentials = self._load_credential_status()

        self.setWindowTitle(f"Configure {connection.display_name}")
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = build_dialog_content(
            self,
            "connectionDialog",
            f"Configure {connection.display_name}",
        )
        form = QFormLayout()
        options = dict(connection.options)
        self._option_inputs: dict[str, QComboBox] = {}
        for field in cache.registry.editable_options(connection.connection_id):
            combo = QComboBox(self)
            combo.setObjectName(f"option_{field.key}")
            for value, label in field.choices:
                combo.addItem(label, value)
            combo.setCurrentIndex(combo.findData(options[field.key]))
            self._option_inputs[field.key] = combo
            form.addRow(field.label, combo)

        self._credential_inputs: dict[str, QLineEdit] = {}
        for credential in spec.credential_fields:
            field_input = _secret_input(f"credential_{credential.key}", self)
            if self._has_stored_credentials:
                field_input.setPlaceholderText("Stored - leave blank to keep")
            self._credential_inputs[credential.key] = field_input
            form.addRow(credential.label, field_input)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        values = {key: field.text().strip() for key, field in self._credential_inputs.items()}
        values = {key: value for key, value in values.items() if value}
        if values and len(values) != len(self._credential_inputs):
            show_warning(self, "Incomplete Credentials", "Enter every credential field.")
            return
        if self._credential_inputs and not values and not self._has_stored_credentials:
            show_warning(self, "Credentials Required", "Enter the credentials for this connection.")
            return

        options: dict[str, str] = {}
        for key, combo in self._option_inputs.items():
            value = combo.currentData()
            if not isinstance(value, str):
                show_warning(self, "Options Required", "Select a value for every connection option.")
                return
            options[key] = value
        try:
            self._cache.set_provider_connection_options(self._connection.connection_id, options)
        except Exception as exc:
            context = ErrorContext(f"Saving {self._connection.display_name} options")
            show_warning(self, "Connection Options Error", report_error(_LOGGER, exc, context))
            return

        if values:
            try:
                self._credential_store.put(self._connection.connection_id, ProviderCredentials(values))
            except RuntimeError as exc:
                context = ErrorContext(f"Saving {self._connection.display_name} credentials")
                show_warning(self, "Credential Store Error", report_error(_LOGGER, exc, context))
                return
        super().accept()

    def done(self, result: int) -> None:
        for field in self._credential_inputs.values():
            field.clear()
        super().done(result)

    def _load_credential_status(self) -> bool:
        try:
            return self._cache.registry.credentials_for(
                self._connection.connection_id, self._credential_store,
            ) is not None
        except RuntimeError:
            return False


class ApplicationSettingsDialog(QDialog):
    def __init__(
        self,
        cache: Cache,
        credential_store: CredentialStore,
        provider_availability: Mapping[str, ProviderAvailability],
        parent: QWidget | None = None,
        active_connection_id: str | None = None,
        connection_dialog_factory: Callable[
            [ProviderConnection, Cache, CredentialStore, QWidget | None],
            ConnectionDialog,
        ] = ConnectionDialog,
        current_cache_namespace: str | None = None,
        current_symbol: str | None = None,
        current_timeframe: Timeframe | None = None,
        comparison_service: BarComparisonServiceLike | None = None,
        symbol_refresh: SymbolRefresher | None = None,
        theme_names: Sequence[str] = ("Light", "Dark"),
    ) -> None:
        super().__init__(parent)
        self._theme_names = theme_names
        self._cache = cache
        self._credential_store = credential_store
        self._provider_availability = dict(provider_availability)
        self._active_connection_id = active_connection_id
        self._source_selected = False
        self._connection_dialog_factory = connection_dialog_factory
        self._selected_connection_id: str | None = None
        self._bars_changed = False

        self.setWindowTitle("Application Settings")
        self.setModal(True)
        self.resize(1000, 780)
        layout = build_dialog_content(
            self,
            "applicationSettingsDialog",
            "Application Settings",
        )
        self.setSizeGripEnabled(True)
        self._tabs = QTabWidget(self)
        self._tabs.setObjectName("applicationSettingsTabs")
        self._tabs.addTab(self._build_connections_tab(), "Connections")
        self._tabs.addTab(self._build_ui_tab(), "UI")
        active_id = (
            active_connection_id
            if active_connection_id is not None
            else cache.get_active_provider_connection_id()
        )
        active_connection = cache.get_provider_connection(active_id) or cache.get_provider_connection(
            cache.registry.fallback_connection_id
        )
        initial_namespace = (
            current_cache_namespace
            if current_cache_namespace is not None
            else (
                None
                if active_connection is None
                else active_connection.cache_namespace
            )
        )
        self._data_quality = DataQualityTab(
            cache,
            self,
            comparison_service=comparison_service,
            initial_cache_namespace=initial_namespace,
            initial_symbol=current_symbol,
            initial_timeframe=current_timeframe,
            symbol_refresh=symbol_refresh,
        )
        self._data_quality.bars_changed.connect(self._on_bars_changed)
        self._tabs.addTab(self._data_quality, "Data Quality")
        layout.addWidget(self._tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def done(self, result: int) -> None:
        self._data_quality.shutdown_comparison()
        super().done(result)

    def accept(self) -> None:
        connection_id = self._active_source.currentData()
        if not isinstance(connection_id, str):
            raise RuntimeError("No active data source is selected.")
        connection = self._cache.get_provider_connection(connection_id)
        if connection is None:
            raise RuntimeError(f"Unknown provider connection: {connection_id}")
        availability = self._availability(connection)
        if not availability.available:
            return
        spec, _ = self._cache.registry.connection_template(connection_id)
        if spec.requires_credentials:
            try:
                credentials = self._cache.registry.credentials_for(connection_id, self._credential_store)
            except RuntimeError as exc:
                context = ErrorContext(
                    f"Reading {connection.display_name} credentials"
                )
                show_warning(
                    self,
                    "Credential Store Error",
                    report_error(_LOGGER, exc, context),
                )
                return
            if credentials is None:
                show_warning(
                    self,
                    "Connection Not Configured",
                    f"Configure {connection.display_name} before selecting it.",
                )
                return
        self._selected_connection_id = connection_id
        choice = self._basis_change_choice.currentData()
        self._cache.set_basis_change_choice(
            None if choice is None else BasisChangeChoice(choice)
        )
        self._cache.set_show_last_price_line(self._show_last_price_line.isChecked())
        self._cache.set_chart_event_visibility(
            self._show_past_earnings.isChecked(), self._show_dividends.isChecked(),
        )
        if self._theme.currentText() != self._initial_theme:
            self._cache.set_theme(self._theme.currentText())
        super().accept()

    def selected_connection_id(self) -> str:
        if self._selected_connection_id is None:
            raise RuntimeError("Application settings were not accepted.")
        return self._selected_connection_id

    def source_was_selected(self) -> bool:
        return self._source_selected

    def _mark_source_selected(self, _index: int) -> None:
        self._source_selected = True

    def bars_changed(self) -> bool:
        return self._bars_changed

    def _on_bars_changed(self) -> None:
        self._bars_changed = True

    def _build_connections_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Active data source", tab))
        self._active_source = QComboBox(tab)
        self._active_source.setObjectName("activeDataSource")
        active_id = (
            self._active_connection_id
            if self._active_connection_id is not None
            else self._cache.get_active_provider_connection_id()
        )
        for connection in self._cache.get_provider_connections():
            self._active_source.addItem(connection.display_name, connection.connection_id)
            availability = self._availability(connection)
            item = self._active_source_item(self._active_source.count() - 1)
            item.setEnabled(availability.available)
            if availability.reason is not None:
                item.setToolTip(availability.reason)
        index = self._active_source.findData(active_id)
        if index < 0 or not self._active_source_item(index).isEnabled():
            index = self._active_source.findData(self._cache.registry.fallback_connection_id)
        self._active_source.setCurrentIndex(index)
        self._active_source.activated.connect(self._mark_source_selected)
        source_row.addWidget(self._active_source, 1)
        layout.addLayout(source_row)

        self._connections = QTableWidget(tab)
        self._connections.setObjectName("providerConnections")
        self._connections.setColumnCount(5)
        self._connections.setHorizontalHeaderLabels(
            ["Connection", "Environment", "Feed", "Credentials", ""]
        )
        vertical_header = self._connections.verticalHeader()
        horizontal_header = self._connections.horizontalHeader()
        assert vertical_header is not None
        assert horizontal_header is not None
        vertical_header.setVisible(False)
        self._connections.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._connections.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        horizontal_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            horizontal_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        layout.addWidget(self._connections)
        self._refresh_connections()
        return tab

    def _build_ui_tab(self) -> QWidget:
        tab = QWidget(self)
        form = QFormLayout(tab)
        self._theme = QComboBox(tab)
        self._theme.setObjectName("theme")
        self._theme.addItems(list(self._theme_names))
        self._theme.setCurrentText(self._cache.get_theme())
        self._initial_theme = self._theme.currentText()
        theme_row = QWidget(tab)
        theme_layout = QHBoxLayout(theme_row)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        theme_layout.addWidget(self._theme)
        theme_hint = QLabel("Applies on next launch", theme_row)
        theme_hint.setObjectName("themeHint")
        theme_layout.addWidget(theme_hint)
        theme_layout.addStretch(1)
        form.addRow("Theme", theme_row)
        self._show_last_price_line = QCheckBox(tab)
        self._show_last_price_line.setObjectName("showLastPriceLine")
        self._show_last_price_line.setToolTip(
            "A line from the newest candle's close to the price axis, with "
            "the price on the axis."
        )
        self._show_last_price_line.setChecked(self._cache.get_show_last_price_line())
        form.addRow("Last price line", self._show_last_price_line)
        earnings, dividends = self._cache.get_chart_event_visibility()
        self._show_past_earnings = QCheckBox("Show past earnings on chart", tab)
        self._show_past_earnings.setObjectName("showPastEarnings")
        self._show_past_earnings.setChecked(earnings)
        form.addRow(self._show_past_earnings)
        self._show_dividends = QCheckBox("Show dividends on chart", tab)
        self._show_dividends.setObjectName("showDividends")
        self._show_dividends.setChecked(dividends)
        form.addRow(self._show_dividends)
        self._basis_change_choice = QComboBox(tab)
        self._basis_change_choice.setObjectName("basisChangeChoice")
        self._basis_change_choice.setToolTip(
            "When a data source adjusts its price history for a split or "
            "dividends, cached history older than it still provides stays "
            "on the old basis."
        )
        self._basis_change_choice.addItem("Ask each time")
        self._basis_change_choice.addItem(
            "Rescale it to match",
            BasisChangeChoice.RESCALE.value,
        )
        self._basis_change_choice.addItem(
            "Discard it",
            BasisChangeChoice.DISCARD.value,
        )
        choice = self._cache.get_basis_change_choice()
        if choice is not None:
            self._basis_change_choice.setCurrentIndex(
                self._basis_change_choice.findData(choice.value)
            )
        form.addRow("Older adjusted history", self._basis_change_choice)
        return tab

    def _refresh_connections(self) -> None:
        connections = self._cache.get_provider_connections()
        self._connections.setRowCount(len(connections))
        for row, connection in enumerate(connections):
            availability = self._availability(connection)
            name_item = QTableWidgetItem(connection.display_name)
            self._set_item_availability(name_item, availability)
            self._connections.setItem(row, 0, name_item)
            connection_id = connection.connection_id
            spec, _ = self._cache.registry.connection_template(connection_id)
            editable = self._cache.registry.editable_options(connection_id)
            options = dict(connection.options)
            fixed_labels = [
                dict(field.choices)[options[field.key]]
                for field in spec.option_fields if field not in editable
            ]
            editable_labels = [dict(field.choices)[options[field.key]] for field in editable]
            environment_item = QTableWidgetItem(", ".join(fixed_labels) or "-")
            feed_item = QTableWidgetItem(", ".join(editable_labels) or "-")
            credential_item = QTableWidgetItem(
                self._credential_status(connection, availability)
            )
            for column, item in enumerate(
                (environment_item, feed_item, credential_item),
                start=1,
            ):
                self._set_item_availability(item, availability)
                self._connections.setItem(row, column, item)
            if spec.requires_credentials or editable:
                configure = QPushButton("Configure", self._connections)
                configure.setObjectName(f"configure_{connection.connection_id}")
                configure.setEnabled(availability.available)
                if availability.reason is not None:
                    configure.setToolTip(availability.reason)
                configure.clicked.connect(
                    lambda _checked=False, selected=connection: self._configure_connection(
                        selected
                    )
                )
                action = QWidget(self._connections)
                action_layout = QHBoxLayout(action)
                action_layout.setContentsMargins(0, 0, 0, 0)
                action_layout.setSpacing(4)
                action_layout.addWidget(configure)
                if not availability.available:
                    info = QToolButton(action)
                    info.setObjectName(f"unavailable_{connection.connection_id}")
                    info.setAutoRaise(True)
                    info.setAccessibleName(
                        f"Why {connection.display_name} is unavailable"
                    )
                    style = self.style()
                    if style is None:
                        raise RuntimeError("The application style is unavailable.")
                    info.setIcon(
                        style.standardIcon(
                            QStyle.StandardPixmap.SP_MessageBoxInformation
                        )
                    )
                    info.setToolTip(availability.reason or "Provider unavailable")
                    info.clicked.connect(
                        lambda _checked=False,
                        name=connection.display_name,
                        reason=availability.reason: show_information(
                            self,
                            "Provider Unavailable",
                            f"{name}\n\n{reason}",
                        )
                    )
                    action_layout.addWidget(info)
                self._connections.setCellWidget(row, 4, action)
        self._connections.resizeRowsToContents()

    def _credential_status(
        self,
        connection: ProviderConnection,
        availability: ProviderAvailability,
    ) -> str:
        spec, _ = self._cache.registry.connection_template(connection.connection_id)
        if not spec.requires_credentials:
            return "Not required"
        if not availability.available:
            return "Unavailable"
        try:
            credentials = self._cache.registry.credentials_for(connection.connection_id, self._credential_store)
        except RuntimeError:
            return "Keyring unavailable"
        return "Stored" if credentials is not None else "Not configured"

    def _availability(
        self,
        connection: ProviderConnection,
    ) -> ProviderAvailability:
        return self._provider_availability.get(
            connection.provider_name,
            ProviderAvailability(False, "Provider availability was not checked."),
        )

    def _active_source_item(self, index: int) -> QStandardItem:
        model = self._active_source.model()
        if not isinstance(model, QStandardItemModel):
            raise RuntimeError("The active data source model is unsupported.")
        item = model.item(index)
        if item is None:
            raise RuntimeError("The active data source item is missing.")
        return item

    @staticmethod
    def _set_item_availability(
        item: QTableWidgetItem,
        availability: ProviderAvailability,
    ) -> None:
        if availability.available:
            return
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        if availability.reason is not None:
            item.setToolTip(availability.reason)

    def _configure_connection(self, connection: ProviderConnection) -> None:
        dialog = self._connection_dialog_factory(
            connection,
            self._cache,
            self._credential_store,
            self,
        )
        dialog.exec()
        self._refresh_connections()


def _secret_input(object_name: str, parent: QWidget) -> QLineEdit:
    field = QLineEdit(parent)
    field.setObjectName(object_name)
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field
