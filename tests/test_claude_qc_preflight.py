import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from devtools.claude_qc.environment import (
    FIXED_CLAUDE_ENVIRONMENT,
    PROHIBITED_CLAUDE_ENVIRONMENT_KEYS,
    build_claude_environment,
)
from devtools.claude_qc.output import parse_review_output
from devtools.claude_qc.packet import MODEL
from devtools.claude_qc.preflight import (
    PREFLIGHT_TIMEOUT_SECONDS,
    REQUIRED_CLAUDE_FLAGS,
    PreflightCategory,
    PreflightError,
    preflight_claude_code,
    resolve_claude_code,
)
from devtools.claude_qc.process import (
    ProcessBackend,
    ProcessOutcome,
    ProcessRequest,
    ProcessTimeoutError,
    SubprocessBackend,
)


class _FakeBackend(ProcessBackend):
    def __init__(self, outcomes: list[ProcessOutcome | Exception]) -> None:
        self.outcomes = outcomes
        self.requests: list[ProcessRequest] = []

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _RecordingEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self._data = values
        self.reads: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.reads.append(key)
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("environment mapping was enumerated")

    def __len__(self) -> int:
        return len(self._data)


def _outcome(stdout: bytes, return_code: int = 0) -> ProcessOutcome:
    return ProcessOutcome(return_code=return_code, stdout=stdout, stderr=b"")


def _auth_status(**changes: object) -> bytes:
    values: dict[str, object] = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "pro",
        "analyticsDisabled": False,
        "email": "private@example.invalid",
        "orgId": "private-org",
        "orgName": "Private organization",
        "projectsDirectory": "/home/private/projects",
    }
    values.update(changes)
    return json.dumps(values).encode("utf-8")


def _successful_backend() -> _FakeBackend:
    help_text = " ".join(REQUIRED_CLAUDE_FLAGS).encode("utf-8")
    return _FakeBackend(
        [
            _outcome(b"2.1.252 (Claude Code)\n"),
            _outcome(help_text),
            _outcome(_auth_status()),
        ]
    )


def _finder(path: str) -> Callable[[str], str | None]:
    def find(name: str) -> str | None:
        assert name == "claude"
        return path

    return find


def test_environment_is_built_from_a_positive_allowlist_without_secret_reads() -> None:
    values = {
        "HOME": "/home/tester",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    values.update(
        {key: "SYNTHETIC_OVERRIDE" for key in PROHIBITED_CLAUDE_ENVIRONMENT_KEYS}
    )
    source = _RecordingEnvironment(values)

    child = build_claude_environment(source, platform="linux")

    assert child == {
        "HOME": "/home/tester",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        **FIXED_CLAUDE_ENVIRONMENT,
    }
    assert set(source.reads) == {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
    }


def test_windows_environment_uses_only_required_runtime_locations() -> None:
    values = {
        "USERPROFILE": r"C:\Users\tester",
        "APPDATA": r"C:\Users\tester\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
        "PROGRAMDATA": r"C:\ProgramData",
        "SystemRoot": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "PATH": r"C:\Windows\System32",
        "PATHEXT": ".COM;.EXE",
        "TEMP": r"C:\Temp",
        "TMP": r"C:\Temp",
    }
    values.update(
        {key: "SYNTHETIC_OVERRIDE" for key in PROHIBITED_CLAUDE_ENVIRONMENT_KEYS}
    )
    source = _RecordingEnvironment(values)

    child = build_claude_environment(source, platform="win32")

    assert child == {
        "USERPROFILE": r"C:\Users\tester",
        "APPDATA": r"C:\Users\tester\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
        "PROGRAMDATA": r"C:\ProgramData",
        "SystemRoot": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "PATH": r"C:\Windows\System32",
        "PATHEXT": ".COM;.EXE",
        "TEMP": r"C:\Temp",
        "TMP": r"C:\Temp",
        **FIXED_CLAUDE_ENVIRONMENT,
    }
    assert set(source.reads).isdisjoint(PROHIBITED_CLAUDE_ENVIRONMENT_KEYS)


def test_subprocess_backend_uses_fixed_argv_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    request = ProcessRequest(
        argv=("/opt/claude", "--version"),
        cwd=tmp_path,
        environment={"HOME": "/home/tester"},
        stdin=b"",
        timeout_seconds=10.0,
    )

    outcome = SubprocessBackend().run(request)

    assert outcome == ProcessOutcome(0, b"ok", b"")
    assert captured["argv"] == request.argv
    assert captured["shell"] is False
    assert captured["input"] == b""
    assert captured["capture_output"] is True
    assert captured["cwd"] == tmp_path
    assert captured["env"] == {"HOME": "/home/tester"}
    assert captured["timeout"] == 10.0


def test_subprocess_timeout_discards_raw_child_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = b"SYNTHETIC_SECRET_DO_NOT_EXPOSE_6B2F"

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("claude", 10.0, output=secret, stderr=secret)

    monkeypatch.setattr(subprocess, "run", timeout)
    request = ProcessRequest(
        argv=("/opt/claude", "--version"),
        cwd=tmp_path,
        environment={},
        stdin=b"",
        timeout_seconds=10.0,
    )

    with pytest.raises(ProcessTimeoutError) as caught:
        SubprocessBackend().run(request)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret.decode() not in repr(caught.value.args)


def test_subprocess_backend_streams_stdout_lines_and_retains_final_output(
    tmp_path: Path,
) -> None:
    lines: list[bytes] = []
    request = ProcessRequest(
        argv=(
            sys.executable,
            "-c",
            "print('first', flush=True); print('second', flush=True)",
        ),
        cwd=tmp_path,
        environment={},
        stdin=b"",
        timeout_seconds=10.0,
        stdout_line_handler=lines.append,
    )

    outcome = SubprocessBackend().run(request)

    assert outcome.return_code == 0
    assert outcome.stdout == b"first\nsecond\n"
    assert lines == [b"first\n", b"second\n"]


def test_streaming_subprocess_round_trips_large_valid_review_output(
    tmp_path: Path,
) -> None:
    finding = {
        "finding_id": "CQ-001",
        "severity": "medium",
        "claim": "Synthetic transport finding.",
        "evidence": "x" * 2_000_000,
        "impact": "The transport must preserve the complete response.",
        "falsification_check": "Compare every output byte and parse the stream.",
        "suggested_disposition": "accept",
        "confidence": "high",
    }
    structured_output = {"findings": [finding]}
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1234,
            "num_turns": 2,
            "stop_reason": "tool_use",
            "permission_denials": [],
            "modelUsage": {MODEL: {"outputTokens": 20}},
            "structured_output": structured_output,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    observed_lines: list[bytes] = []
    request = ProcessRequest(
        argv=(
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ),
        cwd=tmp_path,
        environment={},
        stdin=raw,
        timeout_seconds=10.0,
        stdout_line_handler=observed_lines.append,
    )

    outcome = SubprocessBackend().run(request)
    result = parse_review_output(
        outcome.stdout,
        expected_version="2.1.318",
    )

    assert outcome.return_code == 0
    assert outcome.stdout == raw
    assert b"".join(observed_lines) == raw
    assert result.findings[0].evidence == finding["evidence"]


def test_streaming_subprocess_handles_early_exit_with_large_input(
    tmp_path: Path,
) -> None:
    request = ProcessRequest(
        argv=(sys.executable, "-c", "raise SystemExit(1)"),
        cwd=tmp_path,
        environment={},
        stdin=b"x" * 1_000_000,
        timeout_seconds=1.0,
        stdout_line_handler=lambda line: None,
    )

    outcome = SubprocessBackend().run(request)

    assert outcome.return_code == 1


def test_streaming_subprocess_deadline_includes_unread_input(
    tmp_path: Path,
) -> None:
    request = ProcessRequest(
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        environment={},
        stdin=b"x" * 1_000_000,
        timeout_seconds=0.1,
        stdout_line_handler=lambda line: None,
    )
    started = time.monotonic()

    with pytest.raises(ProcessTimeoutError):
        SubprocessBackend().run(request)

    assert time.monotonic() - started < 3.0


def test_streaming_subprocess_timeout_terminates_pipe_inheriting_child(
    tmp_path: Path,
) -> None:
    child_code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "time.sleep(30)"
    )
    request = ProcessRequest(
        argv=(sys.executable, "-c", child_code),
        cwd=tmp_path,
        environment={},
        stdin=b"",
        timeout_seconds=0.1,
        stdout_line_handler=lambda line: None,
    )
    started = time.monotonic()

    with pytest.raises(ProcessTimeoutError):
        SubprocessBackend().run(request)

    assert time.monotonic() - started < 3.0


def test_resolve_claude_code_reports_missing_executable() -> None:
    with pytest.raises(PreflightError) as caught:
        resolve_claude_code(lambda name: None)

    assert caught.value.category is PreflightCategory.UNAVAILABLE


def test_preflight_accepts_only_compatible_first_party_personal_subscription() -> None:
    backend = _successful_backend()
    parent_environment = {"HOME": "/home/tester", "PATH": "/usr/bin"}

    installation = preflight_claude_code(
        backend,
        parent_environment,
        finder=_finder("/opt/claude"),
    )

    assert installation.executable == Path("/opt/claude")
    assert installation.version == "2.1.252"
    assert installation.subscription_type == "pro"
    assert [request.argv for request in backend.requests] == [
        ("/opt/claude", "--version"),
        ("/opt/claude", "--help"),
        (
            "/opt/claude",
            "auth",
            "status",
            "--json",
        ),
    ]
    assert all(request.stdin == b"" for request in backend.requests)
    assert all(
        request.timeout_seconds == PREFLIGHT_TIMEOUT_SECONDS
        for request in backend.requests
    )
    assert len({request.cwd for request in backend.requests}) == 1
    assert not backend.requests[0].cwd.exists()
    assert all(
        request.environment
        == build_claude_environment(parent_environment, platform="linux")
        for request in backend.requests
    )


def test_preflight_accepts_current_cli_config_directory_field() -> None:
    backend = _FakeBackend(
        [
            _outcome(b"2.1.314 (Claude Code)"),
            _outcome(" ".join(REQUIRED_CLAUDE_FLAGS).encode()),
            _outcome(
                _auth_status(configDirectory="/home/private/.claude")
            ),
        ]
    )

    installation = preflight_claude_code(
        backend,
        {"HOME": "/home/tester", "PATH": "/usr/bin"},
        finder=_finder("/opt/claude"),
    )

    assert installation.version == "2.1.314"
    assert installation.subscription_type == "pro"


def test_preflight_accepts_current_cli_login_expiration_field() -> None:
    backend = _FakeBackend(
        [
            _outcome(b"2.1.318 (Claude Code)"),
            _outcome(" ".join(REQUIRED_CLAUDE_FLAGS).encode()),
            _outcome(
                _auth_status(
                    configDirectory="/home/private/.claude",
                    loginExpiresAt="2026-12-01T12:00:00.000Z",
                )
            ),
        ]
    )

    installation = preflight_claude_code(
        backend,
        {"HOME": "/home/tester", "PATH": "/usr/bin"},
        finder=_finder("/opt/claude"),
    )

    assert installation.version == "2.1.318"
    assert installation.subscription_type == "pro"


def test_preflight_rejects_help_substring_false_positives() -> None:
    misleading_help = "\n".join(
        [
            *(f"  {flag}-removed <value>" for flag in REQUIRED_CLAUDE_FLAGS),
            "  This prose mentions --restricted but does not declare it.",
        ]
    ).encode("utf-8")
    backend = _FakeBackend(
        [
            _outcome(b"2.1.252 (Claude Code)"),
            _outcome(misleading_help),
            _outcome(_auth_status()),
        ]
    )

    with pytest.raises(PreflightError) as caught:
        preflight_claude_code(backend, {}, finder=_finder("/opt/claude"))

    assert caught.value.category is PreflightCategory.INCOMPATIBLE


@pytest.mark.parametrize(
    "auth_status",
    [
        _auth_status(extraAuthSource="unknown"),
        _auth_status(configDirectory=3),
        _auth_status(loginExpiresAt="2026-12-01T12:00:00.000Z"),
        _auth_status(
            configDirectory="/home/private/.claude",
            loginExpiresAt=3,
        ),
        _auth_status().replace(
            b'"loggedIn": true',
            b'"loggedIn": false, "loggedIn": true',
        ),
    ],
)
def test_preflight_accepts_unknown_auth_status_fields(
    auth_status: bytes,
) -> None:
    backend = _FakeBackend(
        [
            _outcome(b"2.1.252 (Claude Code)"),
            _outcome(" ".join(REQUIRED_CLAUDE_FLAGS).encode()),
            _outcome(auth_status),
        ]
    )

    if b'"loggedIn": false, "loggedIn": true' in auth_status:
        with pytest.raises(PreflightError) as caught:
            preflight_claude_code(backend, {}, finder=_finder("/opt/claude"))
        assert caught.value.category is PreflightCategory.SUBSCRIPTION
    else:
        installation = preflight_claude_code(
            backend,
            {},
            finder=_finder("/opt/claude"),
        )
        assert installation.subscription_type == "pro"


@pytest.mark.parametrize(
    "outcomes, category",
    [
        (
            [_outcome(b"not a version"), _outcome(b"unused"), _outcome(b"unused")],
            PreflightCategory.INCOMPATIBLE,
        ),
        (
            [
                _outcome(b"2.1.252 (Claude Code)"),
                _outcome(b"--print"),
                _outcome(b"unused"),
            ],
            PreflightCategory.INCOMPATIBLE,
        ),
        (
            [
                _outcome(b"2.1.252 (Claude Code)"),
                _outcome(" ".join(REQUIRED_CLAUDE_FLAGS).encode()),
                _outcome(_auth_status(authMethod="api_key")),
            ],
            PreflightCategory.SUBSCRIPTION,
        ),
        (
            [
                _outcome(b"2.1.252 (Claude Code)"),
                _outcome(" ".join(REQUIRED_CLAUDE_FLAGS).encode()),
                _outcome(_auth_status(apiProvider="bedrock")),
            ],
            PreflightCategory.SUBSCRIPTION,
        ),
        (
            [
                _outcome(b"2.1.252 (Claude Code)"),
                _outcome(" ".join(REQUIRED_CLAUDE_FLAGS).encode()),
                _outcome(_auth_status(subscriptionType="team")),
            ],
            PreflightCategory.SUBSCRIPTION,
        ),
        (
            [
                _outcome(b"2.1.252 (Claude Code)"),
                _outcome(" ".join(REQUIRED_CLAUDE_FLAGS).encode()),
                _outcome(b"not-json"),
            ],
            PreflightCategory.SUBSCRIPTION,
        ),
    ],
)
def test_preflight_fails_closed_on_incompatible_or_alternate_authentication(
    outcomes: list[ProcessOutcome | Exception],
    category: PreflightCategory,
) -> None:
    backend = _FakeBackend(outcomes)

    with pytest.raises(PreflightError) as caught:
        preflight_claude_code(backend, {}, finder=_finder("/opt/claude"))

    assert caught.value.category is category


def test_preflight_sanitizes_identity_and_process_failure() -> None:
    secret = "SYNTHETIC_SECRET_DO_NOT_EXPOSE_6B2F"
    backend = _FakeBackend(
        [
            _outcome(b"2.1.252 (Claude Code)"),
            _outcome(" ".join(REQUIRED_CLAUDE_FLAGS).encode()),
            _outcome(_auth_status(email=secret), return_code=1),
        ]
    )

    with pytest.raises(PreflightError) as caught:
        preflight_claude_code(
            backend,
            {"ANTHROPIC_API_KEY": secret},
            finder=_finder("/opt/claude"),
        )

    assert caught.value.category is PreflightCategory.SUBSCRIPTION
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value.args)
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith("claude_qc/preflight.py"):
            locals_text = repr(traceback.tb_frame.f_locals)
            assert secret not in locals_text
            assert "ANTHROPIC_API_KEY" not in locals_text
        traceback = traceback.tb_next
