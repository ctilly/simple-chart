"""
app/load_timing.py

Opt-in chart-load timing log for diagnosing load latency in real sessions.

Enabled only when SIMPLECHART_LOAD_TIMING=1 is set at startup. Each record is
one line: wall-clock time, request generation, symbol, timeframe, step name,
and milliseconds since the user action that started the load. Nothing else is
written: no errors, URLs, request parameters, providers, or credentials.

The log lives in a private (0700) directory as a private (0600) file, rotates
at a fixed size, and never affects a load: any setup or write failure disables
it for the rest of the session.
"""

import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from io import TextIOWrapper

LOAD_TIMING_ENV = "SIMPLECHART_LOAD_TIMING"
_LOG_NAME = "load-timing.log"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
_UNSAFE = re.compile(r"[^A-Za-z0-9=.^_-]")


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self) -> TextIOWrapper:
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream

    def handleError(self, record: logging.LogRecord) -> None:
        # Raised to LoadTimingLog.record, which disables the log silently
        # instead of printing logging's default traceback.
        raise OSError("load timing log write failed")


class LoadTimingLog:
    def __init__(
        self,
        path: Path | None,
        max_bytes: int = _MAX_BYTES,
        backup_count: int = _BACKUP_COUNT,
    ) -> None:
        self._handler: _PrivateRotatingFileHandler | None = None
        self._logger: logging.Logger | None = None
        # Serializes writes with disable/close so a failure cannot be
        # followed by another thread reopening the file.
        self._lock = threading.Lock()
        if path is None:
            return
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            handler = _PrivateRotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        except OSError:
            return
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.Logger("simplechart.load_timing")
        logger.propagate = False
        logger.addHandler(handler)
        self._handler = handler
        self._logger = logger

    @classmethod
    def from_environment(
        cls,
        directory: Path,
        environ: Mapping[str, str],
    ) -> "LoadTimingLog":
        if environ.get(LOAD_TIMING_ENV) != "1":
            return cls(None)
        return cls(directory / _LOG_NAME)

    @property
    def enabled(self) -> bool:
        return self._logger is not None

    def record(
        self,
        generation: int,
        symbol: str,
        timeframe: str,
        step: str,
        requested_at: float,
    ) -> None:
        """Append one step; requested_at is time.monotonic() at the user action."""
        if self._logger is None:
            return
        elapsed_ms = (time.monotonic() - requested_at) * 1000.0
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} gen={generation} "
            f"{_UNSAFE.sub('?', symbol)} {_UNSAFE.sub('?', timeframe)} "
            f"{_UNSAFE.sub('_', step)} +{elapsed_ms:.0f}ms"
        )
        with self._lock:
            if self._logger is None:
                return
            try:
                self._logger.info(line)
            except Exception:
                self._close_locked()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        handler = self._handler
        self._handler = None
        self._logger = None
        if handler is not None:
            try:
                handler.close()
            except Exception:
                pass
