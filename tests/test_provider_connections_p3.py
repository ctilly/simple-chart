import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from PyQt6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit, QPushButton, QTableWidget
from pytestqt.qtbot import QtBot

import app.controller as controller
import main as entrypoint
from app import application_settings as settings
from data.cache import Cache
from data.provider.base import DataProvider
from data.provider.config import ProviderConnection
from data.provider.credentials import ProviderCredentials, initialize_keyring_credential_store
from data.provider.registry import FALLBACK_PROVIDER, ProviderAvailability, ProviderRegistry, discover_providers
from data.provider.spec import Capability, ConnectionTemplate, CredentialField, NamespaceSpec, OptionField, ProviderSpec
from providers.alpaca import PROVIDER as ALPACA
from providers.yahoo import PROVIDER as YAHOO
from realistic_errors import SECRET, assert_secret_absent
from ui_text import capture_message_dialogs


_SERVICE = "simplechart.provider-credentials"
_PAPER, _LIVE = (template.connection_id for template in ALPACA.connections)
_PRE_P3_SCHEMA = Path(__file__).parent / "fixtures" / "pre_p3_schema.sql"


class _Keyring:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}
        self.fail_writes = False

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, payload: str) -> None:
        if self.fail_writes:
            raise RuntimeError(payload)
        self.entries[service, username] = payload

    def delete_password(self, service: str, username: str) -> None:
        del self.entries[service, username]


def _legacy_database(path: Path, keyring: _Keyring) -> None:
    with closing(sqlite3.connect(path)) as database, database:
        database.executescript(_PRE_P3_SCHEMA.read_text())
        database.executemany(
            "INSERT INTO provider_connections VALUES (?, ?, ?, ?, ?, ?)",
            [
                (FALLBACK_PROVIDER, "Yahoo Finance", FALLBACK_PROVIDER, None, None, 0),
                (_PAPER, "Alpaca Paper", "alpaca", "paper", "delayed_sip", 1),
                (_LIVE, "Alpaca Live", "alpaca", "live", "invalid-feed", 2),
            ],
        )
        database.execute(
            "INSERT INTO application_settings VALUES ('active_provider_connection', ?)", (_PAPER,),
        )
    keyring.set_password(_SERVICE, _PAPER, json.dumps({"api_key_id": "test-id", "api_secret": SECRET}))


def _database_dump(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as database:
        return list(database.iterdump())


def test_legacy_connections_migrate_once_with_original_credentials(tmp_path: Path) -> None:
    received: list[ProviderCredentials | None] = []

    def create(connection: ProviderConnection, credentials: ProviderCredentials | None) -> DataProvider:
        received.append(credentials)
        return YAHOO.factory(connection, None)

    module = ModuleType("legacy_alpaca")
    module.PROVIDER = replace(ALPACA, factory=create, required_modules=())
    registry = discover_providers(("providers.yahoo", module))
    path = tmp_path / "legacy.db"
    keyring = _Keyring()
    _legacy_database(path, keyring)
    entries_before = keyring.entries.copy()
    access = initialize_keyring_credential_store(keyring, registry=registry)
    assert access.available
    with Cache(str(path), registry=registry) as cache:
        yahoo, paper, live = cache.get_provider_connections()
        assert (yahoo.connection_id, paper.connection_id, live.connection_id) == (FALLBACK_PROVIDER, _PAPER, _LIVE)
        assert dict(paper.options) == {"environment": "paper", "feed": "delayed_sip"}
        assert dict(live.options) == {"environment": "live", "feed": "iex"}
        assert paper.cache_namespace == "alpaca:delayed_sip"
        assert cache.get_active_provider_connection_id() == _PAPER
        registry.create_provider(paper, access.store)
        assert len(received) == 1
        credentials = received[0]
        assert credentials is not None
        assert credentials.get("api_key_id") == "test-id"
        assert credentials.get("api_secret") == SECRET
    migrated = _database_dump(path)
    with closing(sqlite3.connect(path)) as observer:
        version_before = observer.execute("PRAGMA data_version").fetchone()
        with Cache(str(path), registry=registry):
            assert observer.execute("PRAGMA data_version").fetchone() == version_before
    assert _database_dump(path) == migrated
    assert keyring.entries == entries_before


def test_failed_migration_rolls_back_the_options_column(
    tmp_path: Path, provider_registry: ProviderRegistry,
) -> None:
    path = tmp_path / "legacy.db"
    _legacy_database(path, _Keyring())
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(
            "CREATE TRIGGER fail_migration BEFORE UPDATE ON provider_connections "
            "BEGIN SELECT RAISE(ABORT, 'test migration interruption'); END"
        )
    before = _database_dump(path)
    with pytest.raises(sqlite3.IntegrityError, match="test migration interruption"):
        Cache(str(path), registry=provider_registry)
    assert _database_dump(path) == before
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("DROP TRIGGER fail_migration")
    with Cache(str(path), registry=provider_registry) as cache:
        paper = cache.get_provider_connection(_PAPER)
        assert paper is not None
        assert dict(paper.options)["feed"] == "delayed_sip"


@pytest.mark.parametrize("migrate_before_skip", [False, True])
def test_missing_provider_is_hidden_for_one_session_then_restored(
    migrate_before_skip: bool,
    tmp_path: Path,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    provider_registry: ProviderRegistry,
) -> None:
    path = tmp_path / "legacy.db"
    keyring = _Keyring()
    _legacy_database(path, keyring)
    if migrate_before_skip:
        with Cache(str(path), registry=provider_registry):
            pass
    skip = discover_providers(("providers.yahoo",))
    monkeypatch.setattr(
        controller, "initialize_keyring_credential_store",
        lambda *, registry: initialize_keyring_credential_store(keyring, registry=registry),
    )
    monkeypatch.setattr(controller.MainWindow, "_load", lambda self: None)
    monkeypatch.setattr(controller.MainWindow, "_refresh_watchlist_snapshots", lambda self: None)
    monkeypatch.setattr(controller.MainWindow, "_refresh_level1", lambda self: None)
    window = controller.MainWindow(str(path), provider_registry=skip)
    qtbot.addWidget(window)
    assert window._selected_route.connection.connection_id == FALLBACK_PROVIDER

    def accept_settings(dialog: settings.ApplicationSettingsDialog) -> int:
        table = dialog.findChild(QTableWidget, "providerConnections")
        active = dialog.findChild(QComboBox, "activeDataSource")
        assert table is not None and table.rowCount() == 1
        assert active is not None and active.currentData() == FALLBACK_PROVIDER
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(settings.ApplicationSettingsDialog, "exec", accept_settings)
    window._on_application_settings()
    assert window._cache.get_active_provider_connection_id() == _PAPER
    window.close()
    with Cache(str(path), registry=provider_registry) as cache:
        assert cache.get_active_provider_connection_id() == _PAPER
        paper = cache.get_provider_connection(_PAPER)
        assert paper is not None and dict(paper.options)["feed"] == "delayed_sip"
        access = initialize_keyring_credential_store(keyring, registry=provider_registry)
        credentials = provider_registry.credentials_for(_PAPER, access.store)
        assert credentials is not None and credentials.get("api_secret") == SECRET


def test_skipped_providers_row_cannot_be_claimed_by_another_provider(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_database(path, _Keyring())
    source = ModuleType("new_provider_with_colliding_id")
    source.PROVIDER = replace(
        ALPACA,
        name=_PAPER,
        connections=(ConnectionTemplate(_PAPER, "Different provider"),),
        namespaces=(NamespaceSpec(_PAPER, "Different provider", (), False),),
        required_modules=(),
    )
    registry = discover_providers(("providers.yahoo", source))
    with Cache(str(path), registry=registry) as cache:
        assert cache.get_provider_connection(_PAPER) is None
        assert [connection.connection_id for connection in cache.get_provider_connections()] == [
            FALLBACK_PROVIDER,
        ]
        assert cache.get_active_provider_connection_id() == _PAPER
        with pytest.raises(ValueError, match="Unknown provider connection"):
            cache.set_provider_connection_options(_PAPER, {"feed": "sip"})
    with closing(sqlite3.connect(path)) as database:
        row = database.execute(
            "SELECT display_name, provider_name, options FROM provider_connections WHERE connection_id = ?",
            (_PAPER,),
        ).fetchone()
    assert row == ("Alpaca Paper", ALPACA.name, "{}")


@pytest.fixture
def other_registry() -> ProviderRegistry:
    source = ModuleType("test_source")
    source.PROVIDER = ProviderSpec(
        name="testsource",
        display_name="Test Source",
        connections=(ConnectionTemplate("testsource-work", "Test Connection"),),
        capabilities=frozenset({Capability.BARS}),
        namespaces=(NamespaceSpec("testsource", "Test Source", (), True),),
        factory=YAHOO.factory,
        credential_fields=(CredentialField("token", "Access token"),),
        option_fields=(OptionField("tier", "Access tier", (("slow", "Basic"), ("fast", "Fast")), "slow"),),
    )
    return discover_providers(("providers.yahoo", source))


@pytest.mark.parametrize(("journal_mode", "sidecar"), [("WAL", "-wal"), ("PERSIST", "-journal")])
def test_settings_configures_one_field_and_keeps_old_entry_on_keyring_failure(
    journal_mode: str,
    sidecar: str,
    other_registry: ProviderRegistry,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "options.db"
    keyring = _Keyring()
    access = initialize_keyring_credential_store(keyring, registry=other_registry)
    assert access.available
    messages = capture_message_dialogs(monkeypatch)
    credentials = (SECRET, f"replacement-{SECRET}")
    with Cache(str(path), registry=other_registry) as cache:
        assert cache._conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode.lower()
        outer = settings.ApplicationSettingsDialog(
            cache, access.store,
            {name: ProviderAvailability(True, None) for name in other_registry.providers},
        )
        qtbot.addWidget(outer)
        configure = outer.findChild(QPushButton, "configure_testsource-work")
        assert configure is not None

        def exercise(dialog: QDialog) -> int:
            inputs = dialog.findChildren(QLineEdit)
            assert len(inputs) == 1
            token = inputs[0]
            assert token.echoMode() == QLineEdit.EchoMode.Password
            combos = dialog.findChildren(QComboBox)
            assert len(combos) == 1
            tier = combos[0]
            assert [tier.itemText(i) for i in range(tier.count())] == ["Basic", "Fast"]
            expected_tier = "slow" if keyring.fail_writes else "fast"
            if keyring.fail_writes:
                assert token.placeholderText() == "Stored - leave blank to keep"
            token.setText(credentials[keyring.fail_writes])
            tier.setCurrentIndex(tier.findData(expected_tier))
            dialog.accept()
            assert token.text() == (credentials[1] if keyring.fail_writes else "")
            assert (dialog.result() == QDialog.DialogCode.Accepted) != keyring.fail_writes
            updated = cache.get_provider_connection("testsource-work")
            assert updated is not None and dict(updated.options) == {"tier": expected_tier}
            stored = other_registry.credentials_for("testsource-work", access.store)
            assert stored is not None and stored.get("token") == credentials[0]
            assert_secret_absent(repr(stored))
            for file in (path, Path(f"{path}{sidecar}")):
                assert_secret_absent(file.read_bytes().decode("latin1"))
            return dialog.result()

        monkeypatch.setattr(QDialog, "exec", exercise)
        configure.click()
        table = outer.findChild(QTableWidget, "providerConnections")
        assert table is not None
        assert (item := table.item(1, 2)) is not None and item.text() == "Fast"
        assert (item := table.item(1, 3)) is not None and item.text() == "Stored"
        keyring.fail_writes = True
        configure = outer.findChild(QPushButton, "configure_testsource-work")
        assert configure is not None
        configure.click()
        assert (item := table.item(1, 2)) is not None and item.text() == "Basic"
    assert [(message.title, message.message) for message in messages] == [
        ("Credential Store Error", "Unable to save provider credentials in the OS keyring."),
    ]
    assert_secret_absent(messages[0].all_text)
    assert_secret_absent(caplog.text)


def test_dialog_partial_credentials_and_cancel_clear_inputs(
    tmp_path: Path, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, provider_registry: ProviderRegistry,
) -> None:
    keyring = _Keyring()
    access = initialize_keyring_credential_store(keyring, registry=provider_registry)
    messages = capture_message_dialogs(monkeypatch)
    with Cache(str(tmp_path / "test.db"), registry=provider_registry) as cache:
        connection = cache.get_provider_connection(_PAPER)
        assert connection is not None
        dialog = settings.ConnectionDialog(connection, cache, access.store)
        qtbot.addWidget(dialog)
        first, second = dialog.findChildren(QLineEdit)
        (combo,) = dialog.findChildren(QComboBox)
        combo.setCurrentIndex(1)
        first.setText(SECRET)
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert [message.title for message in messages] == ["Incomplete Credentials"]
        assert first.text() == SECRET
        assert second.text() == ""
        assert cache.get_provider_connection(_PAPER) == connection
        assert keyring.entries == {}
        second.setText(SECRET)
        dialog.reject()
        assert first.text() == second.text() == ""
        assert keyring.entries == {}


@pytest.mark.parametrize("connection_id", ["testsource-work", "unknown"])
def test_cli_validates_connection_from_bootstrapped_registry(
    connection_id: str, other_registry: ProviderRegistry, qapp: QApplication, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def bootstrap() -> ProviderRegistry:
        calls.append("bootstrap")
        return other_registry

    def open_window(*, db_path: str, provider_connection_id: str, provider_registry: ProviderRegistry) -> None:
        assert provider_registry is other_registry
        calls.append(provider_connection_id)
        raise SystemExit(0)

    monkeypatch.setattr(sys, "argv", ["simplechart", "--provider", connection_id, "--db", str(tmp_path / "cli.db")])
    monkeypatch.setattr(entrypoint, "bootstrap", bootstrap)
    monkeypatch.setattr(entrypoint, "QApplication", lambda argv: qapp)
    monkeypatch.setattr(controller, "MainWindow", open_window)
    with pytest.raises(SystemExit) as caught:
        entrypoint.main()
    if connection_id == "unknown":
        assert caught.value.code == 2
        assert calls == ["bootstrap"]
        assert "not registered" in capsys.readouterr().err
    else:
        assert caught.value.code == 0
        assert calls == ["bootstrap", connection_id]
