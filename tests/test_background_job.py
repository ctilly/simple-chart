import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event

import pytest
from PyQt6.QtCore import QThread
from pytestqt.qtbot import QtBot

from app.background_job import LatestJob
from data.errors import UserFacingError

_ACTION = "Testing background work"
_ERROR_TEXT = "synthetic exception details"
_TIMEOUT_MS = 5_000


class _GatedWork:
    def __init__(self, result: object, error: Exception | None = None) -> None:
        self.started = Event()
        self.release = Event()
        self.cancellation: Event
        self._result = result
        self._error = error

    def __call__(self, event: Event) -> object:
        self.cancellation = event
        self.started.set()
        assert self.release.wait(timeout=10), "test never released the worker"
        if self._error is not None:
            raise self._error
        return self._result


@contextmanager
def _run_job(
    qtbot: QtBot, *gates: _GatedWork
) -> Iterator[tuple[LatestJob, list[tuple[object, ...]]]]:
    job = LatestJob(_ACTION)
    signals: list[tuple[object, ...]] = []
    job.succeeded.connect(lambda result: signals.append(("succeeded", result)))
    job.failed.connect(lambda: signals.append(("failed",)))
    job.idle.connect(lambda: signals.append(("idle", job.busy)))
    with qtbot.captureExceptions() as escaped:
        try:
            yield job, signals
        finally:
            if job.busy:
                job.cancel()
            for gate in gates:
                gate.release.set()
            qtbot.waitUntil(lambda: not job.busy, timeout=_TIMEOUT_MS)
    assert escaped == []


def test_latest_submit_wins_and_replaces_pending_work(qtbot: QtBot) -> None:
    first = _GatedWork("stale")
    replaced = _GatedWork("replaced")
    latest = _GatedWork("latest")
    with _run_job(qtbot, first, replaced, latest) as (job, signals):
        assert not job.busy
        job.submit(first)
        assert job.busy
        qtbot.waitUntil(first.started.is_set, timeout=_TIMEOUT_MS)
        assert not first.cancellation.is_set()

        job.submit(replaced)
        assert first.cancellation.is_set()
        job.submit(latest)
        assert not replaced.started.is_set()
        assert not latest.started.is_set()

        first.release.set()
        qtbot.waitUntil(latest.started.is_set, timeout=_TIMEOUT_MS)
        assert job.busy
        assert not replaced.started.is_set()
        assert not latest.cancellation.is_set()
        assert signals == []

        with qtbot.waitSignal(job.idle, timeout=_TIMEOUT_MS):
            latest.release.set()
        assert signals == [("succeeded", "latest"), ("idle", False)]


@pytest.mark.parametrize("raises", [False, True], ids=["result", "exception"])
def test_cancel_drops_pending_work_and_only_emits_idle(
    qtbot: QtBot, caplog: pytest.LogCaptureFixture, raises: bool
) -> None:
    error = ValueError(_ERROR_TEXT) if raises else None
    running = _GatedWork("cancelled", error)
    pending = _GatedWork("dropped")
    with _run_job(qtbot, running, pending) as (job, signals):
        job.submit(running)
        qtbot.waitUntil(running.started.is_set, timeout=_TIMEOUT_MS)
        assert not running.cancellation.is_set()
        job.cancel()
        assert running.cancellation.is_set()
        assert job.busy
        assert signals == []

        job.submit(pending)
        job.cancel()
        with qtbot.waitSignal(job.idle, timeout=_TIMEOUT_MS):
            running.release.set()
        assert not pending.started.is_set()
        assert signals == [("idle", False)]
    assert caplog.records == []


@pytest.mark.parametrize(
    ("error_type", "level"),
    [(ValueError, logging.ERROR), (UserFacingError, logging.WARNING)],
)
def test_exception_emits_failed_and_logs_once(
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    level: int,
) -> None:
    def work(_event: Event) -> object:
        raise error_type(_ERROR_TEXT)

    with _run_job(qtbot) as (job, signals):
        with qtbot.waitSignal(job.idle, timeout=_TIMEOUT_MS):
            job.submit(work)
        assert signals == [("failed",), ("idle", False)]
    record, = caplog.records
    assert record.levelno == level
    assert _ACTION in record.getMessage()
    assert error_type.__name__ in record.getMessage()
    if error_type is ValueError:
        assert _ERROR_TEXT not in record.getMessage()


def test_succeeded_handler_can_submit_without_stray_idle(qtbot: QtBot) -> None:
    second = _GatedWork(None)
    with _run_job(qtbot, second) as (job, signals):
        def submit_again(result: object) -> None:
            if result == "first":
                assert QThread.currentThread() is job.thread()
                assert not job.busy
                job.submit(second)
                assert job.busy

        job.succeeded.connect(submit_again)
        job.submit(lambda _event: "first")
        qtbot.waitUntil(second.started.is_set, timeout=_TIMEOUT_MS)
        assert job.busy
        assert signals == [("succeeded", "first")]

        with qtbot.waitSignal(job.idle, timeout=_TIMEOUT_MS):
            second.release.set()
        assert signals == [
            ("succeeded", "first"),
            ("succeeded", None),
            ("idle", False),
        ]


def test_succeeded_handler_can_cancel_a_reentrant_submit(qtbot: QtBot) -> None:
    second = _GatedWork("cancelled")
    with _run_job(qtbot, second) as (job, signals):
        def submit_and_cancel(result: object) -> None:
            if result == "first":
                job.submit(second)
                job.cancel()

        job.succeeded.connect(submit_and_cancel)
        job.submit(lambda _event: "first")
        qtbot.waitUntil(second.started.is_set, timeout=_TIMEOUT_MS)
        assert second.cancellation.is_set()
        assert signals == [("succeeded", "first")]

        with qtbot.waitSignal(job.idle, timeout=_TIMEOUT_MS):
            second.release.set()
        assert signals == [("succeeded", "first"), ("idle", False)]


def test_nested_event_loop_emits_idle_only_from_latest_finish(qtbot: QtBot) -> None:
    with _run_job(qtbot) as (job, signals):
        def submit_and_wait(result: object) -> None:
            if result == "first":
                with qtbot.waitSignal(job.idle, timeout=_TIMEOUT_MS):
                    job.submit(lambda _event: "second")
                signals.append(("nested wait returned",))

        job.succeeded.connect(submit_and_wait)
        job.submit(lambda _event: "first")
        qtbot.waitUntil(
            lambda: ("nested wait returned",) in signals, timeout=_TIMEOUT_MS
        )
        assert signals == [
            ("succeeded", "first"),
            ("succeeded", "second"),
            ("idle", False),
            ("nested wait returned",),
        ]
