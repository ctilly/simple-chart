from collections.abc import Callable, Mapping

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
from app.data_quality import BarComparisonServiceLike, DataQualityTab
from data.cache import Cache
from data.models import Timeframe
from data.provider import ProviderAvailability
from data.provider.config import (
    MarketDataFeed,
    ProviderConnection,
    YFINANCE_CONNECTION_ID,
)
from data.provider.credentials import CredentialStore, ProviderCredentials


class AlpacaConnectionDialog(QDialog):
    def __init__(
        self,
        connection: ProviderConnection,
        cache: Cache,
        credential_store: CredentialStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if connection.provider_name != "alpaca" or connection.feed is None:
            raise ValueError("AlpacaConnectionDialog requires an Alpaca connection.")

        self._connection = connection
        self._cache = cache
        self._credential_store = credential_store
        self._existing_credentials = self._load_credentials()

        self.setWindowTitle(f"Configure {connection.display_name}")
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = build_dialog_content(
            self,
            "alpacaConnectionDialog",
            f"Configure {connection.display_name}",
        )
        form = QFormLayout()

        self._feed = QComboBox(self)
        self._feed.setObjectName("alpacaFeed")
        for feed in MarketDataFeed:
            self._feed.addItem(feed.display_name, feed)
        self._feed.setCurrentIndex(self._feed.findData(connection.feed))
        form.addRow("Market data feed", self._feed)

        self._api_key_id = _secret_input("alpacaApiKeyId", self)
        self._api_secret = _secret_input("alpacaApiSecret", self)
        if self._existing_credentials is not None:
            placeholder = "Stored - leave blank to keep"
            self._api_key_id.setPlaceholderText(placeholder)
            self._api_secret.setPlaceholderText(placeholder)
        form.addRow("API key ID", self._api_key_id)
        form.addRow("API secret", self._api_secret)
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
        api_key_id = self._api_key_id.text().strip()
        api_secret = self._api_secret.text().strip()
        if bool(api_key_id) != bool(api_secret):
            show_warning(
                self,
                "Incomplete Credentials",
                "Enter both the API key ID and API secret.",
            )
            return
        if not api_key_id and self._existing_credentials is None:
            show_warning(
                self,
                "Credentials Required",
                "Enter the Alpaca API key ID and API secret.",
            )
            return

        if api_key_id:
            try:
                self._credential_store.put(
                    self._connection.connection_id,
                    ProviderCredentials(api_key_id, api_secret),
                )
            except RuntimeError as exc:
                show_warning(self, "Credential Store Error", str(exc))
                return

        feed = self._feed.currentData()
        if not isinstance(feed, MarketDataFeed):
            raise RuntimeError("No Alpaca market-data feed is selected.")
        self._cache.set_provider_connection_feed(
            self._connection.connection_id,
            feed,
        )
        super().accept()

    def _load_credentials(self) -> ProviderCredentials | None:
        try:
            return self._credential_store.get(self._connection.connection_id)
        except RuntimeError:
            return None


class ApplicationSettingsDialog(QDialog):
    def __init__(
        self,
        cache: Cache,
        credential_store: CredentialStore,
        provider_availability: Mapping[str, ProviderAvailability],
        parent: QWidget | None = None,
        active_connection_id: str | None = None,
        alpaca_dialog_factory: Callable[
            [ProviderConnection, Cache, CredentialStore, QWidget | None],
            AlpacaConnectionDialog,
        ] = AlpacaConnectionDialog,
        current_cache_namespace: str | None = None,
        current_symbol: str | None = None,
        current_timeframe: Timeframe | None = None,
        comparison_service: BarComparisonServiceLike | None = None,
    ) -> None:
        super().__init__(parent)
        self._cache = cache
        self._credential_store = credential_store
        self._provider_availability = dict(provider_availability)
        self._active_connection_id = active_connection_id
        self._alpaca_dialog_factory = alpaca_dialog_factory
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
        active_connection = cache.get_provider_connection(active_id)
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
        if connection.provider_name == "alpaca":
            try:
                credentials = self._credential_store.get(connection_id)
            except RuntimeError as exc:
                show_warning(self, "Credential Store Error", str(exc))
                return
            if credentials is None:
                show_warning(
                    self,
                    "Connection Not Configured",
                    f"Configure {connection.display_name} before selecting it.",
                )
                return
        self._selected_connection_id = connection_id
        super().accept()

    def selected_connection_id(self) -> str:
        if self._selected_connection_id is None:
            raise RuntimeError("Application settings were not accepted.")
        return self._selected_connection_id

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
        self._active_source.setCurrentIndex(self._active_source.findData(active_id))
        current = self._active_source_item(self._active_source.currentIndex())
        if not current.isEnabled():
            self._active_source.setCurrentIndex(
                self._active_source.findData(YFINANCE_CONNECTION_ID)
            )
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
        dark_mode = QCheckBox(tab)
        dark_mode.setObjectName("darkMode")
        dark_mode.setEnabled(False)
        form.addRow("Dark Mode", dark_mode)
        return tab

    def _refresh_connections(self) -> None:
        connections = self._cache.get_provider_connections()
        self._connections.setRowCount(len(connections))
        for row, connection in enumerate(connections):
            availability = self._availability(connection)
            name_item = QTableWidgetItem(connection.display_name)
            self._set_item_availability(name_item, availability)
            self._connections.setItem(row, 0, name_item)
            environment = (
                "-" if connection.environment is None else connection.environment.value.title()
            )
            feed = "-" if connection.feed is None else connection.feed.display_name
            environment_item = QTableWidgetItem(environment)
            feed_item = QTableWidgetItem(feed)
            credential_item = QTableWidgetItem(
                self._credential_status(connection, availability)
            )
            for column, item in enumerate(
                (environment_item, feed_item, credential_item),
                start=1,
            ):
                self._set_item_availability(item, availability)
                self._connections.setItem(row, column, item)
            if connection.provider_name == "alpaca":
                configure = QPushButton("Configure", self._connections)
                configure.setObjectName(f"configure_{connection.connection_id}")
                configure.setEnabled(availability.available)
                if availability.reason is not None:
                    configure.setToolTip(availability.reason)
                configure.clicked.connect(
                    lambda _checked=False, selected=connection: self._configure_alpaca(
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
        if connection.provider_name == "yfinance":
            return "Not required"
        if not availability.available:
            return "Unavailable"
        try:
            credentials = self._credential_store.get(connection.connection_id)
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

    def _configure_alpaca(self, connection: ProviderConnection) -> None:
        dialog = self._alpaca_dialog_factory(
            connection,
            self._cache,
            self._credential_store,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_connections()


def _secret_input(object_name: str, parent: QWidget) -> QLineEdit:
    field = QLineEdit(parent)
    field.setObjectName(object_name)
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field
