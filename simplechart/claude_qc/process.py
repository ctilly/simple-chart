from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProcessTimeoutError(RuntimeError):
    pass


class ProcessStartError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    stdin: bytes
    timeout_seconds: float


@dataclass(frozen=True, repr=False)
class ProcessOutcome:
    return_code: int
    stdout: bytes
    stderr: bytes


class ProcessBackend(Protocol):
    def run(self, request: ProcessRequest) -> ProcessOutcome: ...


class SubprocessBackend:
    def run(self, request: ProcessRequest) -> ProcessOutcome:
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
            raise ProcessTimeoutError("The Claude Code process deadline expired.")
        if failed_to_start:
            raise ProcessStartError("The Claude Code process could not start.")
        if completed is None:
            raise ProcessStartError("The Claude Code process did not complete.")
        return ProcessOutcome(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
