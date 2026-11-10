"""Process-wide output safety.

Production hooks are exercised only in subprocesses (OS-level stdout/stderr
capture and exit status); in-process tests never call install().
"""

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest

from data.cache import Cache
from data.models import Timeframe
from realistic_errors import SECRET, assert_secret_absent, unknown_error
from simplechart.output_safety import SafeFormatter


_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _ROOT / "tests"
_TIMEOUT_SECONDS = 60
# Code compiled with this filename has app origin; the file is never written.
_APP_ORIGIN = _ROOT / "app" / "_output_safety_probe.py"

_INSTALL = """
import logging
import sys

from realistic_errors import SECRET
from simplechart.output_safety import install

install()


def app_module(source):
    namespace = {}
    exec(compile(source, sys.argv[1], "exec"), namespace)
    return namespace
"""


def _environment(home: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("SIMPLECHART")
    }
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        QT_QPA_PLATFORM="offscreen",
        PYTHONPATH=os.pathsep.join((str(_ROOT), str(_TESTS))),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


def _run(
    tmp_path: Path,
    command: list[str],
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        command,
        cwd=tmp_path,
        env=_environment(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def _run_probe(
    tmp_path: Path,
    source: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "probe.py"
    script.write_text(_INSTALL + dedent(source), encoding="utf-8")
    return _run(tmp_path, [sys.executable, str(script), str(_APP_ORIGIN), *args])


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------


def test_app_log_with_each_real_error_writes_only_safe_facts(tmp_path: Path) -> None:
    facts_path = tmp_path / "facts.json"
    result = _run_probe(
        tmp_path,
        '''
        import json
        from pathlib import Path

        from data.errors import UserFacingError, describe
        from realistic_errors import SOURCES, LocalErrorServer

        app = app_module(
            "import logging\\n"
            "def log_failure(label, exc):\\n"
            "    logging.getLogger('app.loader').error("
            "'Loading %s failed', label, exc_info=exc)\\n"
        )


        def chain_messages(exc):
            chain = []
            current = exc
            while current is not None and all(current is not seen for seen in chain):
                chain.append(current)
                current = current.__cause__ or current.__context__
            # describe() writes only the described UserFacingError's own text.
            if isinstance(exc, UserFacingError):
                chain = chain[1:]
            return [str(item) for item in chain]


        facts = {}
        with LocalErrorServer() as server:
            for label, factory in SOURCES.items():
                exc = factory(server)
                app["log_failure"](label, exc)
                facts[label] = {
                    "describe": describe(exc),
                    "messages": chain_messages(exc),
                }
        Path(sys.argv[2]).write_text(json.dumps(facts), encoding="utf-8")
        ''',
        str(facts_path),
    )
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    facts_path.unlink()

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert len(facts) == 25
    for label, fact in facts.items():
        assert f"app.loader ERROR at _output_safety_probe.py:3 Loading {label} failed" in output
        # Type names along the chain, HTTP status and code locations.
        assert fact["describe"] in output, label
        for message in fact["messages"]:
            if message:
                assert message not in output, label
    for label, status in (
        ("requests_http_401", 401),
        ("curl_http_429", 429),
        ("alpaca_api_403", 403),
    ):
        assert f"(HTTP {status})" in facts[label]["describe"]
    assert "realistic_errors.py:" in output


def test_third_party_origin_is_metadata_only_even_under_app_logger_names(
    tmp_path: Path,
) -> None:
    (tmp_path / "vendor_lib.py").write_text(
        dedent(
            """
            import logging


            def report(exc, secret):
                logging.getLogger("app.controller").error(
                    "request failed for key %s", secret, exc_info=exc
                )
                logging.getLogger("yfinance").error("%s: %s" % ("AAPL", repr(exc)))
                logging.getLogger("app.controller").warning(f"token={secret} retry")
            """
        ),
        encoding="utf-8",
    )
    result = _run_probe(
        tmp_path,
        """
        from realistic_errors import LocalErrorServer, requests_http_error
        from vendor_lib import report

        with LocalErrorServer() as server:
            exc = requests_http_error(server, 401)
        report(exc, SECRET)
        """,
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert "app.controller ERROR at vendor_lib.py:6 (message withheld)" in output
    assert "yfinance ERROR at vendor_lib.py:9 (message withheld)" in output
    assert "app.controller WARNING at vendor_lib.py:10 (message withheld)" in output
    assert "requests.exceptions.HTTPError (HTTP 401)" in output
    for raw_text in ("request failed", "AAPL", "retry", "Unauthorized"):
        assert raw_text not in output


def test_app_logging_shapes_render_exceptions_through_describe(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        f'''
        from realistic_errors import unknown_error

        app = app_module("""
        import logging


        def emit(exc, secret):
            log = logging.getLogger("app.probe")
            log.error("format arg: %s", exc)
            log.error(exc)
            log.error("mapping arg: %(failure)s", {{"failure": exc}})
            log.warning("credential api_key=%s", secret)
            log.error("with stack", stack_info=True)
            logging.getLogger("app.extra").error(
                "with extra", extra={{"payload": secret, "failure": exc}}
            )
            quiet = logging.getLogger("app.quiet")
            quiet.propagate = False
            quiet.error("non-propagating: %s", exc)
        """)

        extra_handler = logging.StreamHandler(sys.stdout)
        extra_handler.setFormatter(
            logging.Formatter("EXTRA %(message)s payload=%(payload)s failure=%(failure)s")
        )
        extra_logger = logging.getLogger("app.extra")
        extra_logger.addHandler(extra_handler)
        extra_logger.propagate = False

        exc = unknown_error()
        app["emit"](exc, SECRET)  # {SECRET}
        ''',
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert "unexpected failure" not in output
    assert "app.probe ERROR at _output_safety_probe.py:7 format arg: RuntimeError" in output
    assert "app.probe ERROR at _output_safety_probe.py:8 RuntimeError" in output
    assert "app.probe ERROR at _output_safety_probe.py:9 mapping arg: RuntimeError" in output
    assert "credential api_key=[redacted]" in output
    assert "with stack\nstack:\n" in output
    assert f"  at {tmp_path / 'probe.py'}:" in output
    assert "EXTRA with extra payload=[withheld] failure=RuntimeError" in output
    assert "app.quiet ERROR at _output_safety_probe.py:17 non-propagating: RuntimeError" in output
    assert "realistic_errors.py:" in output


def test_exceptions_nested_in_log_arguments_are_described(tmp_path: Path) -> None:
    # A vendor wrapper whose stacklevel attributes the record to app code.
    (tmp_path / "vendor_wrapper.py").write_text(
        dedent(
            """
            import logging


            def report_failures(failures):
                logging.error("request failures: %s", failures, stacklevel=2)
            """
        ),
        encoding="utf-8",
    )
    result = _run_probe(
        tmp_path,
        '''
        from realistic_errors import invalid_header_values, requests_invalid_header

        app = app_module("""
        import logging
        from vendor_wrapper import report_failures


        def emit(exc):
            log = logging.getLogger("app.nested")
            report_failures([exc])
            log.error("list: %s", [exc])
            log.error("nested: %(outer)s", {"outer": {"inner": [exc]}})
            log.error("tuple in list: %s", [(exc, "chunk 1")])
            log.error("set and mapping: %s %s", {exc}, {exc: "failed"})
            log.error([exc])
        """)

        exc = requests_invalid_header(invalid_header_values()["ascii_lf_str"])
        app["emit"](exc)
        ''',
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert "root ERROR at _output_safety_probe.py:8 request failures: ['requests" in output
    for line, prefix in (
        (9, "list: ['requests"),
        (10, "nested: {'inner': ['requests"),
        (11, "tuple in list: [('requests"),
        (12, "set and mapping: {'requests"),
        (13, "['requests"),
    ):
        assert f"app.nested ERROR at _output_safety_probe.py:{line} {prefix}" in output
    assert output.count("requests.exceptions.InvalidHeader") >= 7


def test_log_arguments_past_the_nesting_bound_are_withheld(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        '''
        from realistic_errors import invalid_header_values, requests_invalid_header

        app = app_module("""
        import logging


        def emit(exc):
            deep = [exc]
            for _ in range(50):
                deep = [deep]
            looped = [exc]
            looped.append(looped)
            logging.getLogger("app.bound").error("deep %s wide %s loop %s", deep, [0] * 5000 + [exc], looped)
        """)

        exc = requests_invalid_header(invalid_header_values()["ascii_lf_str"])
        app["emit"](exc)
        ''',
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert (
        "app.bound ERROR at _output_safety_probe.py:11 "
        "deep [withheld] wide [withheld] loop [withheld]"
    ) in output


def test_mapping_argument_without_exceptions_keeps_its_own_behavior(
    tmp_path: Path,
) -> None:
    result = _run_probe(
        tmp_path,
        '''
        app = app_module("""
        import logging
        from collections import defaultdict


        def emit():
            logging.getLogger("app.mapping").error("missing %(missing)s", defaultdict(int, a=1))
        """)

        app["emit"]()
        ''',
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "app.mapping ERROR at _output_safety_probe.py:7 missing 0" in output


def test_handler_added_after_install_sees_only_the_sanitized_record(
    tmp_path: Path,
) -> None:
    result = _run_probe(
        tmp_path,
        """
        from realistic_errors import unknown_error

        app = app_module(
            "import logging\\n"
            "def emit(exc):\\n"
            "    logging.getLogger('app.late').error('app saw %s', exc, exc_info=exc)\\n"
        )


        class BrokenStream:
            def write(self, text):
                raise OSError("disk full")

            def flush(self):
                pass


        class LibraryHandler(logging.StreamHandler):
            def handleError(self, record):
                sys.stderr.write(f"LIBRARY {record.getMessage()} {vars(record)}\\n")


        logging.getLogger().addHandler(LibraryHandler(BrokenStream()))
        exc = unknown_error()
        logging.getLogger("vendor").error("vendor saw %s", SECRET, exc_info=exc)
        app["emit"](exc)
        """,
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert "unexpected failure" not in output
    assert "LIBRARY (message withheld)\nRuntimeError" in output
    assert "LIBRARY app saw RuntimeError" in output
    assert "'exc_info': None" in output
    assert "vendor saw" not in output


def test_installed_handler_fails_closed_on_write_flush_and_format_errors(
    tmp_path: Path,
) -> None:
    result = _run_probe(
        tmp_path,
        """
        class FailingWrite:
            def write(self, text):
                raise OSError(f"write failed {SECRET}")

            def flush(self):
                pass


        class FailingFlush:
            def write(self, text):
                pass

            def flush(self):
                raise OSError(f"flush failed {SECRET}")


        class FailingFormatter(logging.Formatter):
            def format(self, record):
                raise ValueError(f"format failed {SECRET}")


        (root_handler,) = logging.getLogger().handlers
        original = root_handler.stream
        vendor = logging.getLogger("vendor")
        root_handler.stream = FailingWrite()
        vendor.error("one")
        root_handler.stream = FailingFlush()
        vendor.error("two")
        root_handler.stream = original
        root_handler.setFormatter(FailingFormatter())
        vendor.error("three")
        print("survived", flush=True)
        """,
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert result.stderr.count("logging output failed\n") == 3
    assert result.stdout == "survived\n"
    assert "Traceback" not in output


def test_last_resort_handler_is_safe(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        """
        from realistic_errors import unknown_error

        app = app_module(
            "import logging\\n"
            "def emit(exc):\\n"
            "    logging.getLogger('app.lastresort').error('app lost handlers: %s', exc)\\n"
        )

        logging.getLogger().handlers.clear()
        logging.getLogger("vendor").warning("vendor %s", SECRET)
        app["emit"](unknown_error())
        """,
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert "vendor WARNING at probe.py:" in output
    assert "(message withheld)" in output
    assert (
        "app.lastresort ERROR at _output_safety_probe.py:3 app lost handlers: RuntimeError"
        in output
    )
    assert "unexpected failure" not in output


def test_reported_provider_failure_reaches_the_production_handler(
    tmp_path: Path,
) -> None:
    result = _run_probe(
        tmp_path,
        """
        from app.errors import ErrorContext, report_error
        from realistic_errors import PROVIDER_SOURCES, LocalErrorServer

        with LocalErrorServer() as server:
            exc = PROVIDER_SOURCES["alpaca_api_401"](server)
        shown = report_error(
            logging.getLogger("app.controller"),
            exc,
            ErrorContext("Loading AAPL daily history"),
        )
        print("shown:", shown)
        """,
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    assert "shown: Alpaca Paper rejected the credentials." in result.stdout
    assert "app.controller WARNING at errors.py:" in result.stderr
    assert (
        "Loading AAPL daily history failed: "
        "data.provider.base.ProviderCredentialsRejectedError: "
        "Alpaca Paper rejected the credentials."
    ) in result.stderr
    assert "caused by alpaca.common.exceptions.APIError (HTTP 401)" in result.stderr


def test_startup_handler_set_is_the_safe_root_handler(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        """
        import app.controller  # loads built-in and user plugins
        from simplechart.output_safety import SafeFormatter

        (root_handler,) = logging.getLogger().handlers
        print("root formatter safe:", type(root_handler.formatter) is SafeFormatter)
        print("last resort safe:", type(logging.lastResort.formatter) is SafeFormatter)
        print("raise exceptions:", logging.raiseExceptions)
        loggers = logging.getLogger().manager.loggerDict.values()
        print(
            "other handlers:",
            sorted({
                type(handler).__name__
                for logger in loggers
                if isinstance(logger, logging.Logger)
                for handler in logger.handlers
            } - {"NullHandler"}),
        )
        """,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.splitlines() == [
        "root formatter safe: True",
        "last resort safe: True",
        "raise exceptions: False",
        "other handlers: []",
    ]


# ------------------------------------------------------------------
# Other process channels
# ------------------------------------------------------------------


def test_warnings_qt_unraisable_and_thread_channels_write_metadata_only(
    tmp_path: Path,
) -> None:
    result = _run_probe(
        tmp_path,
        """
        import threading
        import warnings

        from PyQt6.QtCore import qCritical, qInfo, qWarning

        warnings.warn(f"token {SECRET}")
        warnings.warn(UserWarning(f"api_key={SECRET}"))
        qInfo(f"info {SECRET}")
        qWarning(f"warning {SECRET}")
        qCritical(f"critical {SECRET}")


        class Leaky:
            def __repr__(self):
                return f"Leaky({SECRET})"

            def __del__(self):
                raise RuntimeError(f"del failed {SECRET}")


        Leaky()


        def worker():
            raise RuntimeError(f"thread failed {SECRET}")


        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        print("survived", flush=True)
        """,
    )

    output = _output(result)
    assert result.returncode == 0, result.stderr[-2000:]
    assert_secret_absent(output)
    probe = tmp_path / "probe.py"
    assert f"UserWarning at {probe}:21 (message withheld)" in output
    assert f"UserWarning at {probe}:22 (message withheld)" in output
    assert f"Qt QtInfoMsg at {probe}:23 (default) (message withheld)" in output
    assert f"Qt QtWarningMsg at {probe}:24 (default) (message withheld)" in output
    assert f"Qt QtCriticalMsg at {probe}:25 (default) (message withheld)" in output
    assert "RuntimeError\n" in output
    assert "in Leaky.__del__" in output
    assert "in worker" in output
    for raw_text in ("del failed", "thread failed", "Leaky(", "info ", "critical "):
        assert raw_text not in output
    assert result.stdout == "survived\n"


def test_unhandled_main_thread_exception_exits_1_with_safe_facts(
    tmp_path: Path,
) -> None:
    result = _run_probe(
        tmp_path,
        """
        import atexit

        atexit.register(lambda: print("atexit ran"))
        print("before failure")


        def fail():
            try:
                raise KeyError(SECRET)
            except KeyError as exc:
                raise RuntimeError(f"wrapped {SECRET}") from exc


        fail()
        """,
    )

    output = _output(result)
    assert result.returncode == 1
    assert_secret_absent(output)
    assert result.stdout == "before failure\n"
    assert "RuntimeError\n" in result.stderr
    assert "caused by KeyError" in result.stderr
    assert "in fail" in result.stderr
    assert "wrapped" not in output
    assert "Traceback" not in output


def test_unhandled_qt_slot_exception_exits_1_not_abort(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        """
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication

        application = QApplication([])


        def slot():
            raise RuntimeError(f"slot failed {SECRET}")


        QTimer.singleShot(0, slot)
        application.exec()
        """,
    )

    output = _output(result)
    assert result.returncode == 1
    assert_secret_absent(output)
    assert "RuntimeError\n" in result.stderr
    assert "in slot" in result.stderr
    assert "slot failed" not in output


def test_fatal_hook_exits_1_when_its_own_output_fails(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        """
        class BrokenStream:
            def write(self, text):
                raise OSError("stream closed")

            def flush(self):
                raise OSError("stream closed")


        sys.stdout = BrokenStream()
        sys.stderr = BrokenStream()
        raise RuntimeError(f"fatal {SECRET}")
        """,
    )

    assert result.returncode == 1
    assert_secret_absent(_output(result))


# ------------------------------------------------------------------
# Entry points
# ------------------------------------------------------------------

_LEAKY_PLUGIN = f"""
import logging

installed = logging.Logger.callHandlers.__module__ == "simplechart.output_safety"
print(f"plugin import: installed={{installed}}", flush=True)
log = logging.getLogger("leaky_plugin")
log.error("plugin import saw %s", RuntimeError("cause {SECRET}"))
log.error("plugin token=%s", "{SECRET}")
raise RuntimeError("plugin failed {SECRET}")
"""

_ENTRY_POINTS = {
    "main": "import sys; from main import main; sys.exit(main())",
    "gui_launcher": "from simplechart.gui_launcher import main; main()",
}


@pytest.mark.parametrize("entry_point", sorted(_ENTRY_POINTS))
def test_entry_point_installs_before_plugin_import(
    tmp_path: Path,
    entry_point: str,
) -> None:
    plugin_dir = tmp_path / "home" / ".simplechart" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "leaky_plugin.py").write_text(_LEAKY_PLUGIN, encoding="utf-8")
    # The failing plugin is skipped and startup continues; a later plugin
    # ends the process once loading has passed it.
    (plugin_dir / "zz_stop.py").write_text("raise SystemExit(1)\n", encoding="utf-8")

    result = _run(
        tmp_path,
        [
            sys.executable,
            "-c",
            _ENTRY_POINTS[entry_point],
            "--db",
            str(tmp_path / "db" / "simplechart.db"),
        ],
    )

    output = _output(result)
    assert result.returncode == 1
    assert_secret_absent(output)
    assert "plugin import: installed=True" in result.stdout
    assert "leaky_plugin ERROR at leaky_plugin.py:7 plugin import saw RuntimeError" in output
    assert "leaky_plugin ERROR at leaky_plugin.py:8 plugin token=[redacted]" in output
    assert "plugin failed" not in output
    assert "Plugin 'leaky_plugin.py' could not be loaded" in result.stderr
    assert f"at {plugin_dir / 'leaky_plugin.py'}:9 in <module>" in result.stderr


def test_fatal_exit_keeps_committed_data_and_written_load_timing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "db" / "simplechart.db"
    probe = tmp_path / "fatal_load.py"
    probe.write_text(
        dedent(
            f"""
            import sys
            from datetime import UTC, datetime
            from pathlib import Path

            from PyQt6.QtCore import QTimer

            import app.controller
            from app.load_timing import LoadTimingLog
            from data.cache import Cache
            from data.models import Bar, Timeframe


            class FatalDuringLoad:
                def __init__(self, db_path, provider_connection_id):
                    self._cache = Cache(db_path)
                    self._cache.put_bars("yfinance", "TEST", Timeframe.DAILY, [
                        Bar(datetime(2026, 11, day, tzinfo=UTC), 10.0, 11.0, 9.0, 10.5, 1000)
                        for day in (2, 3, 4)
                    ])
                    self._timing = LoadTimingLog.from_environment(
                        Path(db_path).parent / "diagnostics",
                        {{"SIMPLECHART_LOAD_TIMING": "1"}},
                    )
                    self._timing.record(1, "TEST", "1d", "bars committed", 0.0)
                    QTimer.singleShot(0, self._fail)

                def _fail(self):
                    raise RuntimeError("fatal during load {SECRET}")

                def setWindowIcon(self, icon):
                    pass

                def show(self):
                    pass


            app.controller.MainWindow = FatalDuringLoad
            from main import main

            sys.argv = ["simplechart", "--db", sys.argv[1]]
            main()
            """
        ),
        encoding="utf-8",
    )

    result = _run(tmp_path, [sys.executable, str(probe), str(db_path)])

    assert result.returncode == 1
    assert_secret_absent(_output(result))
    assert "RuntimeError\n" in result.stderr
    # SQLite recovers the WAL on the next open; nothing deleted it.
    assert Path(f"{db_path}-wal").stat().st_size > 0
    assert Path(f"{db_path}-shm").exists()
    with Cache(str(db_path)) as cache:
        bars = cache.get_bars(
            "yfinance",
            "TEST",
            Timeframe.DAILY,
            0,
            int(datetime(2027, 1, 1, tzinfo=UTC).timestamp() * 1000),
        )
    assert [bar.timestamp.day for bar in bars] == [2, 3, 4]
    timing_lines = (
        (db_path.parent / "diagnostics" / "load-timing.log")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(timing_lines) == 1
    assert timing_lines[0].split(" ")[1:5] == ["gen=1", "TEST", "1d", "bars_committed"]


# ------------------------------------------------------------------
# SafeFormatter (in-process; no install)
# ------------------------------------------------------------------


def _record(
    pathname: str,
    msg: object,
    args: tuple[object, ...] = (),
    exc: BaseException | None = None,
) -> logging.LogRecord:
    exc_info = None if exc is None else (type(exc), exc, exc.__traceback__)
    return logging.LogRecord("app.test", logging.ERROR, pathname, 12, msg, args, exc_info)


def test_safe_formatter_withholds_untrusted_text_and_describes_the_exception() -> None:
    exc = unknown_error()
    record = _record(str(_TESTS / "vendor.py"), "failed %s", (SECRET,), exc)

    text = SafeFormatter("%(message)s").format(record)

    assert_secret_absent(text)
    assert text.startswith("(message withheld)\nRuntimeError\n")
    assert "realistic_errors.py:" in text
    assert record.exc_text is None
    assert record.msg == "failed %s"


def test_safe_formatter_masks_trusted_text_and_describes_exception_args() -> None:
    record = _record(str(_APP_ORIGIN), "token=%s after %s", (SECRET, unknown_error()))

    text = SafeFormatter("%(message)s").format(record)

    assert_secret_absent(text)
    assert text.startswith("token=[redacted] after RuntimeError\n")


def test_safe_formatter_withholds_extra_and_renders_stack_locations_only() -> None:
    record = _record(str(_APP_ORIGIN), "with extras")
    record.payload = SECRET
    record.stack_info = (
        "Stack (most recent call last):\n"
        f'  File "/src/app/x.py", line 3, in load\n'
        f"    load(key={SECRET!r})\n"
    )

    formatter = SafeFormatter("%(message)s %(payload)s")
    text = formatter.format(record)

    assert_secret_absent(text)
    assert text == "with extras\nstack:\n  at /src/app/x.py:3 in load [withheld]"


class _CountingMapping(Mapping[str, object]):
    def __init__(self, size: int) -> None:
        self.size = size
        self.visits = 0

    def __getitem__(self, key: str) -> object:
        self.visits += 1
        return 0

    def __iter__(self) -> Iterator[str]:
        for index in range(self.size):
            self.visits += 1
            yield f"k{index}"

    def __len__(self) -> int:
        return self.size

    def __str__(self) -> str:
        return f"mapping {id(self)}"


def test_mapping_argument_without_exceptions_is_formatted_as_itself() -> None:
    mapping = _CountingMapping(3)
    record = _record(str(_APP_ORIGIN), "args %s", (mapping,))

    text = SafeFormatter("%(message)s").format(record)

    assert text == f"args mapping {id(mapping)}"


def test_oversized_mapping_argument_is_withheld_without_visiting_entries() -> None:
    mapping = _CountingMapping(5000)
    record = _record(str(_APP_ORIGIN), "args %s", (mapping,))

    text = SafeFormatter("%(message)s").format(record)

    assert text == "args [withheld]"
    assert mapping.visits == 0
