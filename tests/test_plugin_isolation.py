"""Plugin isolation below the window: the guard, store attribution,
registration-time ownership and plugin load rollback.

Plugins are real files (or packages) in temporary directories, loaded through
the real loader; their exception messages carry the synthetic secret.
"""

import importlib
import logging
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from typing import Any

import numpy as np
import pytest

import plugin_faults
from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import ChartExtensionStore
from app.state import ChartExtensionState, State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
from indicators.avwap.anchor_store import AvwapAnchorStore
from plugin_faults import (
    TEMP_PACKAGE_PREFIX,
    arm,
    fault_plugin_source,
    isolated_plugin_state,
    plugin_module,
    write_plugin,
)
from realistic_errors import SECRET, assert_secret_absent
from simplechart.api import (
    ChartExtension,
    ChartExtensionMutation,
    all_extensions,
    register_extension,
)
from simplechart.extensions import _guard, _registry, _store_registry
from simplechart.extensions._guard import (
    call_extension,
    call_store_handler,
    is_disabled,
    set_disable_handler,
    take_notices,
)
from simplechart.plugins import (
    _load_plugin_file,
    load_plugin_directory,
    load_plugin_package,
    load_plugins,
)


_FIRST = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
_EXCEPTION_TEXT = "exploded"


@pytest.fixture(autouse=True)
def _isolated_plugins() -> Iterator[None]:
    with isolated_plugin_state():
        yield


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[Cache]:
    with Cache(str(tmp_path / "test.db")) as opened:
        yield opened


def _notice(label: str) -> str:
    return f"'{label}' failed and was turned off. Details were logged."


def _skipped(name: str) -> str:
    return f"Plugin '{name}' could not be loaded and was skipped."


def _assert_shielded(text: str) -> None:
    assert_secret_absent(text)
    assert _EXCEPTION_TEXT not in text


def _series(count: int = 50) -> OHLCVSeries:
    return OHLCVSeries(
        "XBI",
        Timeframe.DAILY,
        [
            Bar(_FIRST + timedelta(days=i), 100.0, 101.0, 99.0, 100.5, 1_000)
            for i in range(count)
        ],
    )


def _temp_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> str:
    name = f"{TEMP_PACKAGE_PREFIX}{uuid.uuid4().hex[:8]}"
    root = tmp_path / "packages"
    for relative, source in files.items():
        path = root / name / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(source))
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    return name


def _registration(name: str, label: str) -> str:
    # Module body registering one extension.
    return f"""
        from typing import Any

        import numpy as np

        import plugin_faults
        from simplechart.api import ChartExtension, OHLCVSeries, register_extension

        plugin_faults.EXECUTIONS.append(__name__)


        class Registered(ChartExtension):
            def name(self) -> str:
                return {name!r}

            def label(self) -> str:
                return {label!r}

            def default_params(self) -> dict[str, Any]:
                return {{}}

            def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, np.ndarray]:
                return {{}}


        register_extension(Registered)
    """


# ------------------------------------------------------------------
# The guard
# ------------------------------------------------------------------


class _InProcessExtension(ChartExtension):
    LABEL_CALLS = 0

    def name(self) -> str:
        return "iso_in_process"

    def label(self) -> str:
        _InProcessExtension.LABEL_CALLS += 1
        return "In-Process Extension"

    def default_params(self) -> dict[str, Any]:
        return {}

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, np.ndarray]:
        return {}


def test_guard_turns_a_failing_extension_off_once_and_never_calls_it_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    register_extension(_InProcessExtension)
    label_calls = _InProcessExtension.LABEL_CALLS
    handled: list[frozenset[str]] = []
    set_disable_handler(handled.append)
    calls: list[str] = []

    def boom() -> int:
        calls.append("boom")
        raise RuntimeError(f"hook {_EXCEPTION_TEXT}: api_key={SECRET}")

    assert call_extension("iso_in_process", lambda: 5) == 5
    caplog.set_level(logging.WARNING)
    assert call_extension("iso_in_process", boom) is None
    assert call_extension("iso_in_process", boom) is None
    assert call_extension("iso_in_process", lambda: 5) is None

    assert calls == ["boom"]
    assert is_disabled("iso_in_process")
    assert take_notices() == [_notice("In-Process Extension")]
    assert take_notices() == []
    assert handled == [frozenset({"iso_in_process"})]
    # The cached label named it; cleanup never called label().
    assert _InProcessExtension.LABEL_CALLS == label_calls
    assert "RuntimeError" in caplog.text
    assert "iso_in_process" in caplog.text
    _assert_shielded(caplog.text)


def test_store_handler_failure_turns_off_every_extension_its_module_registered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    register_extension(_InProcessExtension)
    owner = __name__

    def boom() -> None:
        raise RuntimeError(f"store {_EXCEPTION_TEXT}: {SECRET}")

    assert call_store_handler(owner, boom) is None
    assert is_disabled("iso_in_process")
    assert take_notices() == [_notice("In-Process Extension")]
    assert call_store_handler(owner, lambda: 1) is None
    _assert_shielded(caplog.text)


# ------------------------------------------------------------------
# Store attribution through the production store
# ------------------------------------------------------------------


_STORE_CALLS: dict[str, Callable[[ChartExtensionStore], object]] = {
    "load_for_symbol": lambda store: store.load_for_symbol("XBI"),
    "apply": lambda store: store.apply(
        ChartExtensionMutation("iso_store_b", "noop", {})
    ),
    "prepare_active_extensions": lambda store: store.prepare_active_extensions(),
    "params_for": lambda store: store.params_for("iso_store_b", {"color": "#fff"}),
    "clear_transient": lambda store: store.clear_transient_drawings(),
}


@pytest.mark.parametrize("hook", sorted(_STORE_CALLS))
def test_failing_store_handler_disables_only_its_owner_and_the_loop_continues(
    tmp_path: Path,
    cache: Cache,
    caplog: pytest.LogCaptureFixture,
    hook: str,
) -> None:
    plugins = tmp_path / "plugins"
    write_plugin(
        plugins,
        "a_store",
        fault_plugin_source(name="iso_store_a", label="Store A", store_fail_in=hook),
    )
    write_plugin(plugins, "b_store", fault_plugin_source(name="iso_store_b", label="Store B"))
    load_plugin_directory(plugins)
    failing = plugin_module("a_store")
    healthy = plugin_module("b_store")
    store = ChartExtensionStore(State(), cache)
    arm(failing, healthy)
    caplog.set_level(logging.WARNING)

    _STORE_CALLS[hook](store)

    assert is_disabled("iso_store_a")
    assert not is_disabled("iso_store_b")
    # The healthy handler, registered after the failing one, still ran.
    assert healthy.STORE_CALLS[-1] == hook
    assert take_notices() == [_notice("Store A")]
    failed_calls = list(failing.STORE_CALLS)
    for call in _STORE_CALLS.values():
        call(store)
    assert failing.STORE_CALLS == failed_calls
    _assert_shielded(caplog.text)


def test_failing_store_factory_is_skipped_and_other_handlers_are_created(
    tmp_path: Path,
    cache: Cache,
) -> None:
    plugins = tmp_path / "plugins"
    write_plugin(
        plugins,
        "a_store",
        fault_plugin_source(name="iso_store_a", label="Store A", store_fail_in="factory"),
    )
    write_plugin(plugins, "b_store", fault_plugin_source(name="iso_store_b", label="Store B"))
    load_plugin_directory(plugins)
    arm(plugin_module("a_store"))

    store = ChartExtensionStore(State(), cache)
    params = store.params_for("iso_store_b", {})

    assert is_disabled("iso_store_a")
    assert params == {}
    assert plugin_module("b_store").STORE_CALLS.count("factory") == 1
    assert take_notices() == [_notice("Store A")]


def test_store_failure_while_resolving_another_extensions_params_keeps_it_rendering(
    tmp_path: Path,
    cache: Cache,
) -> None:
    plugins = tmp_path / "plugins"
    write_plugin(
        plugins,
        "a_store",
        fault_plugin_source(name="iso_store_a", label="Store A", store_fail_in="params_for"),
    )
    write_plugin(plugins, "b_store", fault_plugin_source(name="iso_store_b", label="Store B"))
    load_plugin_directory(plugins)
    failing = plugin_module("a_store")
    state = State()
    state.extensions = [
        ChartExtensionState(name="iso_store_b", params={"color": "#fff"}),
        ChartExtensionState(name="iso_store_a", params={"color": "#fff"}),
    ]
    store = ChartExtensionStore(state, cache)
    runtime = ChartExtensionRuntime(state, cache, store, 600)
    arm(failing)

    passes = runtime.render_all(_series())
    calls = list(failing.CALLS)
    passes = runtime.render_all(_series())

    assert [render_pass.state.name for render_pass in passes] == ["iso_store_b"]
    assert failing.CALLS == calls
    assert "render" not in calls
    assert take_notices() == [_notice("Store A")]


# ------------------------------------------------------------------
# Registration-time ownership
# ------------------------------------------------------------------


_SHARED_PLUGIN = """
from typing import Any

import numpy as np

from plugin_faults import register_via_helper, store_handler
from simplechart.api import ChartExtension, OHLCVSeries

ARMED = False
CALLS: list[str] = []


class Shared(ChartExtension):
    def name(self) -> str:
        return {name!r}

    def label(self) -> str:
        return {label!r}

    def default_params(self) -> dict[str, Any]:
        return {{}}

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, np.ndarray]:
        CALLS.append("compute")
        return {{}}


@store_handler
class SharedStore:
    def __init__(self, context: Any) -> None:
        pass

    def load_for_symbol(self, symbol: str) -> None:
        pass

    def apply(self, mutation: Any) -> None:
        pass

    def prepare_active_extensions(self) -> None:
        pass

    def params_for(self, extension_name: str, base_params: dict[str, Any]) -> dict[str, Any]:
        if ARMED:
            raise RuntimeError("shared store exploded: " + {secret!r})
        return base_params


register_via_helper(Shared)
"""


def test_shared_helper_and_decorator_attribute_registrations_to_each_plugin(
    tmp_path: Path,
    cache: Cache,
) -> None:
    plugins = tmp_path / "plugins"
    for stem, name, label in (
        ("shared_a", "iso_shared_a", "Shared A"),
        ("shared_b", "iso_shared_b", "Shared B"),
    ):
        write_plugin(
            plugins,
            stem,
            _SHARED_PLUGIN.format(name=name, label=label, secret=SECRET),
        )
    load_plugin_directory(plugins)
    plugin_a = plugin_module("shared_a")
    plugin_b = plugin_module("shared_b")

    assert _registry._owners["iso_shared_a"] == "simplechart_user_plugins.shared_a"
    assert _registry._owners["iso_shared_b"] == "simplechart_user_plugins.shared_b"
    assert _store_registry.store_handler_owner(plugin_a.SharedStore) == (
        "simplechart_user_plugins.shared_a"
    )
    assert _store_registry.store_handler_owner(plugin_b.SharedStore) == (
        "simplechart_user_plugins.shared_b"
    )

    state = State()
    state.extensions = [
        ChartExtensionState(name="iso_shared_b", params={}),
        ChartExtensionState(name="iso_shared_a", params={}),
    ]
    runtime = ChartExtensionRuntime(state, cache, ChartExtensionStore(state, cache), 600)
    arm(plugin_a)
    # A's store fails while B's params are resolved.
    passes = runtime.render_all(_series())
    a_calls = list(plugin_a.CALLS)
    passes = runtime.render_all(_series())

    assert is_disabled("iso_shared_a")
    assert not is_disabled("iso_shared_b")
    assert [render_pass.state.name for render_pass in passes] == ["iso_shared_b"]
    assert plugin_a.CALLS == a_calls
    assert plugin_b.CALLS.count("compute") == 2


def test_builtin_imported_before_load_plugins_owns_its_registrations(
    cache: Cache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_plugins(user_plugin_dir=None)

    assert _registry._owners["sma"] == "indicators.sma"
    assert _registry._owners["avwap"] == "indicators.avwap"
    assert _store_registry.store_handler_owner(AvwapAnchorStore) == "indicators.avwap"

    def boom(self: object, extension_name: str, base_params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"avwap store {_EXCEPTION_TEXT}: {SECRET}")

    monkeypatch.setattr(AvwapAnchorStore, "params_for", boom)
    store = ChartExtensionStore(State(), cache)
    params = store.params_for("sma", {"days": 5})

    assert params["days"] == 5
    assert {name for name in all_extensions() if is_disabled(name)} == {"avwap"}


def test_nested_import_registrations_belong_to_the_imported_module(
    tmp_path: Path,
    cache: Cache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_source = f"""

        class NestedStore:
            def __init__(self, context: Any) -> None:
                pass

            def load_for_symbol(self, symbol: str) -> None:
                pass

            def apply(self, mutation: Any) -> None:
                pass

            def prepare_active_extensions(self) -> None:
                pass

            def params_for(self, extension_name: str, base_params: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("nested store exploded: " + {SECRET!r})


        from simplechart.api import register_store_handler
        register_store_handler(NestedStore)
    """
    package = _temp_package(
        tmp_path,
        monkeypatch,
        {
            "__init__.py": "",
            "a_mod.py": "from . import _b_helper\n"
            + dedent(_registration("iso_nested_a", "Nested A")),
            "_b_helper.py": dedent(_registration("iso_nested_b", "Nested B"))
            + dedent(store_source),
        },
    )

    load_plugin_package(package)

    assert _registry._owners["iso_nested_a"] == f"{package}.a_mod"
    assert _registry._owners["iso_nested_b"] == f"{package}._b_helper"
    ChartExtensionStore(State(), cache).params_for("iso_nested_a", {})
    assert is_disabled("iso_nested_b")
    assert not is_disabled("iso_nested_a")


def test_registration_outside_an_import_belongs_to_the_calling_module() -> None:
    register_extension(_InProcessExtension)

    assert _registry._owners["iso_in_process"] == __name__


# ------------------------------------------------------------------
# Load rollback
# ------------------------------------------------------------------


class _Original(ChartExtension):
    def name(self) -> str:
        return "iso_overwritten"

    def label(self) -> str:
        return "Original"

    def default_params(self) -> dict[str, Any]:
        return {}

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, np.ndarray]:
        return {}


_BROKEN_FILE = """
from typing import Any

import numpy as np

import plugin_faults
from simplechart.api import (
    ChartExtension,
    OHLCVSeries,
    register_extension,
    register_store_handler,
)

plugin_faults.EXECUTIONS.append(__name__)


class Broken(ChartExtension):
    def name(self) -> str:
        return "iso_broken"

    def label(self) -> str:
        return "Broken"

    def default_params(self) -> dict[str, Any]:
        return {}

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, np.ndarray]:
        return {}


class Overwriter(Broken):
    def name(self) -> str:
        return "iso_overwritten"

    def label(self) -> str:
        return "Overwritten"


class BrokenStore:
    def __init__(self, context: Any) -> None:
        pass


register_extension(Broken)
register_store_handler(BrokenStore)
register_extension(Overwriter)
COMPLETE = True
"""


def test_failed_plugin_file_is_rolled_back_and_later_plugins_load(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    register_extension(_Original)
    extensions_before = _registry.snapshot()
    stores_before = _store_registry.snapshot()
    plugins = tmp_path / "plugins"
    broken = write_plugin(
        plugins,
        "a_broken",
        _BROKEN_FILE + f"raise RuntimeError('import {_EXCEPTION_TEXT}: ' + {SECRET!r})\n",
    )
    write_plugin(plugins, "b_good", _registration("iso_good", "Good"))
    caplog.set_level(logging.WARNING)

    load_plugin_directory(plugins)

    assert "iso_good" in all_extensions()
    assert "iso_broken" not in all_extensions()
    assert all_extensions()["iso_overwritten"] is _Original
    assert _registry._labels["iso_overwritten"] == "Original"
    assert _registry._owners["iso_overwritten"] == __name__
    good = extensions_before.registry | {"iso_good": all_extensions()["iso_good"]}
    assert _registry.snapshot().registry == good
    assert _store_registry.snapshot() == stores_before
    assert "simplechart_user_plugins.a_broken" not in sys.modules
    assert take_notices() == [_skipped("a_broken.py")]
    assert "RuntimeError" in caplog.text
    _assert_shielded(caplog.text)

    # A retry runs the file again rather than returning a partial module.
    executions = plugin_faults.EXECUTIONS.count("simplechart_user_plugins.a_broken")
    assert _load_plugin_file(broken) is None
    assert plugin_faults.EXECUTIONS.count("simplechart_user_plugins.a_broken") == executions + 1
    assert "simplechart_user_plugins.a_broken" not in sys.modules
    assert take_notices() == [_skipped("a_broken.py")]

    broken.write_text(dedent(_BROKEN_FILE))
    module = _load_plugin_file(broken)
    assert module is not None
    assert module.COMPLETE is True
    assert _registry._labels["iso_overwritten"] == "Overwritten"


def test_package_root_failure_rolls_back_the_package_and_its_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    extensions_before = _registry.snapshot()
    package = _temp_package(
        tmp_path,
        monkeypatch,
        {
            "__init__.py": f"""
                import plugin_faults
                plugin_faults.EXECUTIONS.append(__name__)
                from . import child_ok
                raise RuntimeError("root {_EXCEPTION_TEXT}: " + {SECRET!r})
            """,
            "child_ok.py": _registration("iso_root_child", "Root Child"),
        },
    )
    caplog.set_level(logging.WARNING)

    load_plugin_package(package)

    assert _registry.snapshot() == extensions_before
    assert package not in sys.modules
    assert f"{package}.child_ok" not in sys.modules
    assert take_notices() == [_skipped(package)]
    _assert_shielded(caplog.text)

    load_plugin_package(package)
    assert plugin_faults.EXECUTIONS.count(package) == 2
    assert plugin_faults.EXECUTIONS.count(f"{package}.child_ok") == 2
    assert _registry.snapshot() == extensions_before


def test_failed_member_with_nested_helper_and_subpackage_children_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _temp_package(
        tmp_path,
        monkeypatch,
        {
            "__init__.py": "",
            "_helper.py": _registration("iso_helper", "Helper"),
            "a_bad.py": f"""
                from . import _helper
                raise RuntimeError("member {_EXCEPTION_TEXT}: " + {SECRET!r})
            """,
            "b_sub/__init__.py": f"""
                from . import leaf
                raise RuntimeError("subpackage {_EXCEPTION_TEXT}: " + {SECRET!r})
            """,
            "b_sub/leaf.py": _registration("iso_leaf", "Leaf"),
            "c_good.py": _registration("iso_c_good", "C Good"),
        },
    )

    load_plugin_package(package)

    assert "iso_helper" not in all_extensions()
    assert "iso_leaf" not in all_extensions()
    assert "iso_c_good" in all_extensions()
    for module in ("_helper", "a_bad", "b_sub", "b_sub.leaf"):
        assert f"{package}.{module}" not in sys.modules
    assert package in sys.modules
    assert take_notices() == [_skipped(f"{package}.a_bad"), _skipped(f"{package}.b_sub")]

    # The helper's registrations were not lost: a repaired member importing
    # it the same way runs it again rather than getting the rolled-back
    # module from its parent package.
    helper_runs = plugin_faults.EXECUTIONS.count(f"{package}._helper")
    (tmp_path / "packages" / package / "a_bad.py").write_text("from . import _helper\n")
    load_plugin_package(package)
    assert "iso_helper" in all_extensions()
    assert plugin_faults.EXECUTIONS.count(f"{package}._helper") == helper_runs + 1
    assert take_notices() == [_skipped(f"{package}.b_sub")]


def test_registering_module_outside_the_plugin_namespace_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plugin file imports an installed helper whose module body registers,
    # then fails: the helper leaves sys.modules so a retry registers again.
    package = _temp_package(
        tmp_path,
        monkeypatch,
        {
            "__init__.py": "",
            "registers.py": _registration("iso_outside", "Outside"),
        },
    )
    plugins = tmp_path / "plugins"
    write_plugin(
        plugins,
        "uses_helper",
        f"import {package}.registers\n"
        f"raise RuntimeError('helper user {_EXCEPTION_TEXT}: ' + {SECRET!r})\n",
    )

    load_plugin_directory(plugins)

    assert "iso_outside" not in all_extensions()
    assert f"{package}.registers" not in sys.modules
    importlib.import_module(f"{package}.registers")
    assert "iso_outside" in all_extensions()


def test_guard_state_is_reset_between_tests() -> None:
    assert not _guard._disabled
    assert take_notices() == []
