import json
from pathlib import Path

import pytest

from devtools.claude_qc.environment import build_claude_environment
from devtools.claude_qc.findings import FINDINGS_JSON_SCHEMA
from devtools.claude_qc.packet import EFFORT, MODEL
from devtools.claude_qc.preflight import ClaudeCodeInstallation
from devtools.claude_qc.process import (
    ProcessBackend,
    ProcessOutcome,
    ProcessRequest,
    ProcessStartError,
    ProcessTimeoutError,
)
from devtools.claude_qc.runner import (
    REVIEW_DEADLINE_SECONDS,
    REVIEW_INSTRUCTION,
    REVIEW_SYSTEM_PROMPT,
    ReviewProgress,
    ReviewRunCategory,
    ReviewRunError,
    ReviewRunReason,
    ReviewStage,
    build_review_argv,
    run_review,
)


def _result_output(**changes: object) -> bytes:
    finding = {
        "finding_id": "CQ-001",
        "severity": "low",
        "claim": "Synthetic claim.",
        "evidence": "Synthetic evidence.",
        "impact": "Synthetic impact.",
        "falsification_check": "Synthetic check.",
        "suggested_disposition": "defer",
        "confidence": "low",
    }
    value: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 100,
        "num_turns": 2,
        "stop_reason": "tool_use",
        "permission_denials": [],
        "usage": {},
        "modelUsage": {MODEL: {"outputTokens": 20}},
        "structured_output": {"findings": [finding]},
    }
    value.update(changes)
    return json.dumps(value).encode() + b"\n"


class _RecordingBackend(ProcessBackend):
    def __init__(
        self,
        *,
        create_file: bool = False,
        return_code: int = 0,
        output_changes: dict[str, object] | None = None,
    ) -> None:
        self.create_file = create_file
        self.return_code = return_code
        self.output_changes = dict(output_changes or {})
        self.requests: list[ProcessRequest] = []

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        self.requests.append(request)
        if self.create_file:
            (request.cwd / "unexpected-session.json").write_text("unexpected")
        stdout = _result_output(**self.output_changes)
        if request.stdout_line_handler is not None:
            for line in stdout.splitlines(keepends=True):
                request.stdout_line_handler(line)
        return ProcessOutcome(
            return_code=self.return_code,
            stdout=stdout,
            stderr=b"SYNTHETIC_PROVIDER_ERROR",
        )


class _TimeoutBackend(ProcessBackend):
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        self.requests.append(request)
        raise ProcessTimeoutError("synthetic timeout")


class _StartFailureBackend(ProcessBackend):
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        self.requests.append(request)
        raise ProcessStartError("synthetic start failure")


class _ProviderEventBackend(ProcessBackend):
    def run(self, request: ProcessRequest) -> ProcessOutcome:
        stdout = json.dumps(
            {"type": "error", "message": "SYNTHETIC_PROVIDER_ERROR"}
        ).encode()
        return ProcessOutcome(return_code=0, stdout=stdout, stderr=b"")


def _installation() -> ClaudeCodeInstallation:
    return ClaudeCodeInstallation(Path("/opt/claude"), "2.1.252", "pro")


def test_review_argv_is_locked_down_and_contains_no_output_ceiling() -> None:
    argv = build_review_argv(Path("/opt/claude"))

    assert argv[0] == "/opt/claude"
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--effort") + 1] == EFFORT
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--verbose" not in argv
    assert argv[argv.index("--json-schema") + 1] == json.dumps(
        FINDINGS_JSON_SCHEMA,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert argv[argv.index("--system-prompt") + 1] == REVIEW_SYSTEM_PROMPT
    assert argv[-1] == REVIEW_INSTRUCTION
    assert "--restricted" in argv
    assert "--safe-mode" in argv
    assert "--disable-slash-commands" in argv
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--no-chrome" in argv
    assert "--bare" not in argv
    assert "--fallback-model" not in argv
    assert "--max-budget-usd" not in argv
    assert "--max-tokens" not in argv
    assert "--max-turns" not in argv
    assert "--setting-sources" not in argv


def test_review_uses_packet_only_on_stdin_and_one_empty_temporary_directory() -> None:
    backend = _RecordingBackend()
    packet = b'{"synthetic":"packet"}'
    parent_environment = {"HOME": "/home/tester", "PATH": "/usr/bin"}

    result = run_review(
        _installation(),
        packet,
        backend,
        parent_environment,
        platform="linux",
    )

    assert result.findings[0].finding_id == "CQ-001"
    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert request.stdin == packet
    assert packet.decode() not in " ".join(request.argv)
    assert request.timeout_seconds == REVIEW_DEADLINE_SECONDS
    assert request.environment == build_claude_environment(
        parent_environment,
        platform="linux",
    )
    assert not request.cwd.exists()
    assert "synthetic" not in repr(request)


def test_review_reports_safe_progress_without_raw_event_content() -> None:
    progress: list[ReviewProgress] = []

    run_review(
        _installation(),
        b"packet",
        _RecordingBackend(),
        {},
        platform="linux",
        progress=progress.append,
    )

    assert progress == [
        ReviewProgress.STARTING,
        ReviewProgress.FINAL_RESPONSE,
    ]


def test_review_rejects_nonzero_process_without_raw_error() -> None:
    backend = _RecordingBackend(return_code=1)

    with pytest.raises(ReviewRunError) as caught:
        run_review(_installation(), b"packet", backend, {}, platform="linux")

    assert caught.value.category is ReviewRunCategory.PROVIDER
    assert caught.value.stage is ReviewStage.FINAL_RESPONSE
    assert "SYNTHETIC_PROVIDER_ERROR" not in str(caught.value)
    assert len(backend.requests) == 1


def test_review_failure_traceback_does_not_retain_sensitive_inputs() -> None:
    packet_secret = "SYNTHETIC_PACKET_SECRET_6B2F"
    environment_secret = "SYNTHETIC_ENVIRONMENT_SECRET_91C4"
    backend = _RecordingBackend(return_code=1)

    with pytest.raises(ReviewRunError) as caught:
        run_review(
            _installation(),
            packet_secret.encode(),
            backend,
            {"HOME": environment_secret},
            platform="linux",
        )

    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith("claude_qc/runner.py"):
            locals_text = repr(traceback.tb_frame.f_locals)
            assert packet_secret not in locals_text
            assert environment_secret not in locals_text
            assert "SYNTHETIC_PROVIDER_ERROR" not in locals_text
        traceback = traceback.tb_next


def test_review_maps_deadline_without_wrapper_retry() -> None:
    backend = _TimeoutBackend()

    with pytest.raises(ReviewRunError) as caught:
        run_review(_installation(), b"packet", backend, {}, platform="linux")

    assert caught.value.category is ReviewRunCategory.DEADLINE
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(backend.requests) == 1


def test_review_maps_start_failure_without_wrapper_retry() -> None:
    backend = _StartFailureBackend()

    with pytest.raises(ReviewRunError) as caught:
        run_review(_installation(), b"packet", backend, {}, platform="linux")

    assert caught.value.category is ReviewRunCategory.PROVIDER
    assert len(backend.requests) == 1


def test_review_maps_provider_event_without_raw_error() -> None:
    with pytest.raises(ReviewRunError) as caught:
        run_review(
            _installation(),
            b"packet",
            _ProviderEventBackend(),
            {},
            platform="linux",
        )

    assert caught.value.category is ReviewRunCategory.PROVIDER
    assert "SYNTHETIC_PROVIDER_ERROR" not in str(caught.value)


def test_review_rejects_any_file_created_in_isolated_cwd() -> None:
    backend = _RecordingBackend(create_file=True)

    with pytest.raises(ReviewRunError) as caught:
        run_review(_installation(), b"packet", backend, {}, platform="linux")

    assert caught.value.category is ReviewRunCategory.UNSAFE
    assert caught.value.reason is ReviewRunReason.UNEXPECTED_LOCAL_ARTIFACT


def test_review_rejects_permission_denial_without_raw_output() -> None:
    backend = _RecordingBackend(
        output_changes={
            "permission_denials": [{"tool": "SYNTHETIC_UNAUTHORIZED_TOOL"}]
        }
    )

    with pytest.raises(ReviewRunError) as caught:
        run_review(_installation(), b"packet", backend, {}, platform="linux")

    assert caught.value.category is ReviewRunCategory.UNSAFE
    assert "SYNTHETIC_UNAUTHORIZED_TOOL" not in str(caught.value)
