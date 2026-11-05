from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


class ProcessTimeoutError(RuntimeError):
    pass


class ProcessStartError(RuntimeError):
    pass


StdoutLineHandler = Callable[[bytes], None]


@dataclass(frozen=True, repr=False)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    stdin: bytes
    timeout_seconds: float
    stdout_line_handler: StdoutLineHandler | None = None


@dataclass(frozen=True, repr=False)
class ProcessOutcome:
    return_code: int
    stdout: bytes
    stderr: bytes


class ProcessBackend(Protocol):
    def run(self, request: ProcessRequest) -> ProcessOutcome: ...


def _read_pipe(
    pipe: BinaryIO,
    output: bytearray,
    line_handler: StdoutLineHandler | None,
) -> None:
    for line in iter(pipe.readline, b""):
        output.extend(line)
        if line_handler is not None:
            try:
                line_handler(line)
            except Exception:
                pass


class SubprocessBackend:
    def run(self, request: ProcessRequest) -> ProcessOutcome:
        if request.stdout_line_handler is None:
            completed: subprocess.CompletedProcess[bytes] | None = None
            timed_out = False
            failed_to_start = False
            try:
                completed = subprocess.run(
                    request.argv,
                    input=request.stdin,
                    capture_output=True,
                    cwd=request.cwd,
                    env=dict(request.environment),
                    timeout=request.timeout_seconds,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
            except OSError:
                failed_to_start = True
            if timed_out:
                raise ProcessTimeoutError(
                    "The Claude Code process deadline expired."
                )
            if failed_to_start or completed is None:
                raise ProcessStartError(
                    "The Claude Code process could not start."
                )
            return ProcessOutcome(
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        with tempfile.TemporaryFile() as packet_file:
            packet_file.write(request.stdin)
            packet_file.seek(0)
            process: subprocess.Popen[bytes] | None = None
            failed_to_start = False
            try:
                process = subprocess.Popen(
                    request.argv,
                    stdin=packet_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=request.cwd,
                    env=dict(request.environment),
                    shell=False,
                    start_new_session=os.name != "nt",
                )
            except OSError:
                failed_to_start = True
            if failed_to_start or process is None:
                raise ProcessStartError(
                    "The Claude Code process could not start."
                )
            if process.stdout is None or process.stderr is None:
                raise ProcessStartError(
                    "The Claude Code process did not complete."
                )

            stdout = bytearray()
            stderr = bytearray()
            stdout_thread = threading.Thread(
                target=_read_pipe,
                args=(process.stdout, stdout, request.stdout_line_handler),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_read_pipe,
                args=(process.stderr, stderr, None),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            timed_out = False
            try:
                return_code = process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
                process.wait()
                return_code = process.returncode
            finally:
                _finish_readers(process, stdout_thread, stderr_thread)
            if timed_out:
                raise ProcessTimeoutError(
                    "The Claude Code process deadline expired."
                )
        return ProcessOutcome(
            return_code=return_code,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
        )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _finish_readers(
    process: subprocess.Popen[bytes],
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
) -> None:
    stdout_thread.join(timeout=1.0)
    stderr_thread.join(timeout=1.0)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _terminate_process_group(process)
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
