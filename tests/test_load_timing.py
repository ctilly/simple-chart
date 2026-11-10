import os
import stat
from pathlib import Path

import pytest

from app.load_timing import LOAD_TIMING_ENV, LoadTimingLog
from realistic_errors import SECRET, assert_secret_absent, unknown_error
from simplechart.output_safety import SafeFormatter


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_disabled_by_default_touches_no_files(tmp_path: Path) -> None:
    directory = tmp_path / "diagnostics"

    log = LoadTimingLog.from_environment(directory, {})
    log.record(1, "XBI", "5m", "load requested", 0.0)

    assert not log.enabled
    assert not directory.exists()


def test_enabled_log_is_private_and_contains_only_allow_listed_fields(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "diagnostics"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    old_umask = os.umask(0o022)
    try:
        log = LoadTimingLog.from_environment(directory, {LOAD_TIMING_ENV: "1"})
        enabled = log.enabled
        log.record(7, "XBI", "5m", "recent delivered", 0.0)
        log.close()
    finally:
        os.umask(old_umask)

    path = directory / "load-timing.log"
    assert enabled
    assert _mode(directory) == 0o700
    assert _mode(path) == 0o600
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    fields = lines[0].split(" ")
    assert fields[1:5] == ["gen=7", "XBI", "5m", "recent_delivered"]
    assert fields[5].startswith("+") and fields[5].endswith("ms")
    assert len(fields) == 6


def test_symbol_and_step_cannot_inject_log_lines(tmp_path: Path) -> None:
    directory = tmp_path / "diagnostics"
    log = LoadTimingLog.from_environment(directory, {LOAD_TIMING_ENV: "1"})

    log.record(1, "XBI\nfake line", "5m", "step\nother", 0.0)
    log.close()

    lines = (directory / "load-timing.log").read_text().splitlines()
    assert len(lines) == 1
    assert "\n" not in lines[0]


def test_log_rotates_within_bounds_and_backups_stay_private(tmp_path: Path) -> None:
    directory = tmp_path / "diagnostics"
    log = LoadTimingLog(directory / "load-timing.log", max_bytes=400, backup_count=3)

    for generation in range(200):
        log.record(generation, "XBI", "5m", "render done", 0.0)
    log.close()

    files = sorted(directory.iterdir())
    assert [path.name for path in files] == [
        "load-timing.log",
        "load-timing.log.1",
        "load-timing.log.2",
        "load-timing.log.3",
    ]
    for path in files:
        assert path.stat().st_size <= 400
        assert _mode(path) == 0o600


def test_unusable_location_disables_without_raising(tmp_path: Path) -> None:
    blocker = tmp_path / "diagnostics"
    blocker.write_text("not a directory")

    log = LoadTimingLog.from_environment(blocker, {LOAD_TIMING_ENV: "1"})
    log.record(1, "XBI", "5m", "load requested", 0.0)

    assert not log.enabled


def test_write_failure_disables_log_silently(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "diagnostics"
    log = LoadTimingLog.from_environment(directory, {LOAD_TIMING_ENV: "1"})
    log.record(1, "XBI", "5m", "load requested", 0.0)

    def failing_emit(_record: object) -> None:
        raise OSError("disk full")

    log._handler.emit = failing_emit  # type: ignore[method-assign,assignment,union-attr]
    log.record(2, "XBI", "5m", "load requested", 0.0)
    log.record(3, "XBI", "5m", "load requested", 0.0)

    assert not log.enabled
    assert capsys.readouterr().err == ""


def test_write_failure_on_one_thread_is_never_followed_by_a_write(
    tmp_path: Path,
) -> None:
    import threading

    directory = tmp_path / "diagnostics"
    log = LoadTimingLog.from_environment(directory, {LOAD_TIMING_ENV: "1"})
    handler = log._handler
    assert handler is not None
    in_emit = threading.Event()
    release = threading.Event()
    written: list[str] = []

    def emit(record: object) -> None:
        if not written:
            written.append("first")
            in_emit.set()
            assert release.wait(5)
            raise OSError("disk full")
        written.append("later")

    handler.emit = emit  # type: ignore[method-assign,assignment]
    first = threading.Thread(target=log.record, args=(1, "XBI", "5m", "a", 0.0))
    second = threading.Thread(target=log.record, args=(2, "XBI", "5m", "b", 0.0))
    first.start()
    assert in_emit.wait(5)
    second.start()
    second.join(0.1)
    assert second.is_alive()  # serialized behind the in-progress write
    release.set()
    first.join(5)
    second.join(5)

    assert written == ["first"]
    assert not log.enabled


def test_log_lines_go_through_the_safe_formatter(tmp_path: Path) -> None:
    directory = tmp_path / "diagnostics"
    log = LoadTimingLog.from_environment(directory, {LOAD_TIMING_ENV: "1"})
    assert log._handler is not None and log._logger is not None
    assert type(log._handler.formatter) is SafeFormatter

    log._logger.error("failed with %s", SECRET, exc_info=unknown_error())
    log.record(1, "XBI", "5m", "load requested", 0.0)
    log.close()

    text = (directory / "load-timing.log").read_text()
    assert_secret_absent(text)
    assert "(message withheld)\nRuntimeError\n" in text
    assert " gen=1 XBI 5m load_requested +" in text
