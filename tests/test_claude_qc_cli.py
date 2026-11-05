import io
import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from devtools.claude_qc.packet import (
    MODEL,
    PACKET_FIELDS,
    canonical_packet_bytes,
    packet_digest,
    parse_packet,
)
from devtools.claude_qc.preflight import (
    REQUIRED_CLAUDE_FLAGS,
    ClaudeCodeInstallation,
    PreflightCategory,
    PreflightError,
)
from devtools.claude_qc.process import ProcessBackend, ProcessOutcome, ProcessRequest
from devtools.claude_qc.runner import (
    ReviewProgress,
    ReviewRunCategory,
    ReviewRunError,
    ReviewRunReason,
    ReviewStage,
)


def _packet_raw() -> bytes:
    values = {field: f"synthetic {field}" for field in PACKET_FIELDS}
    values.update(
        {
            "schema_version": "1",
            "review_kind": "design",
            "implementation_diff": "",
            "verification_summary": "",
        }
    )
    return json.dumps(values).encode()


def _auth_status() -> bytes:
    return json.dumps(
        {
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
    ).encode()


def _finding() -> dict[str, str]:
    return {
        "finding_id": "CQ-001",
        "severity": "low",
        "claim": "Synthetic claim.",
        "evidence": "Synthetic evidence.",
        "impact": "Synthetic impact.",
        "falsification_check": "Synthetic check.",
        "suggested_disposition": "defer",
        "confidence": "low",
    }


def _result_output() -> bytes:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 100,
            "num_turns": 2,
            "stop_reason": "tool_use",
            "permission_denials": [],
            "usage": {},
            "modelUsage": {MODEL: {"outputTokens": 20}},
            "structured_output": {"findings": [_finding()]},
        }
    ).encode()


class _WorkflowBackend(ProcessBackend):
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        self.requests.append(request)
        index = len(self.requests)
        if index == 1:
            return ProcessOutcome(0, b"2.1.252 (Claude Code)\n", b"")
        if index == 2:
            return ProcessOutcome(
                0,
                " ".join(REQUIRED_CLAUDE_FLAGS).encode(),
                b"",
            )
        if index == 3:
            return ProcessOutcome(0, _auth_status(), b"")
        output = _result_output()
        if request.stdout_line_handler is not None:
            request.stdout_line_handler(output)
        return ProcessOutcome(0, output, b"")


class _ForbiddenBackend(ProcessBackend):
    def run(self, request: ProcessRequest) -> ProcessOutcome:
        raise AssertionError("Claude access was not expected")


class _InterruptBackend(ProcessBackend):
    def run(self, request: ProcessRequest) -> ProcessOutcome:
        raise KeyboardInterrupt


_DEEPLY_NESTED_JSON = (b"[" * 1500) + (b"]" * 1500)


class _DeepAuthBackend(ProcessBackend):
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ProcessOutcome(0, b"2.1.252 (Claude Code)\n", b"")
        if len(self.requests) == 2:
            return ProcessOutcome(
                0,
                " ".join(REQUIRED_CLAUDE_FLAGS).encode(),
                b"",
            )
        return ProcessOutcome(0, _DEEPLY_NESTED_JSON, b"")


class _DeepOutputBackend(_WorkflowBackend):
    def run(self, request: ProcessRequest) -> ProcessOutcome:
        if len(self.requests) == 3:
            self.requests.append(request)
            return ProcessOutcome(0, _DEEPLY_NESTED_JSON, b"")
        return super().run(request)


class _NoRead(io.BytesIO):
    def read(self, _size: int | None = -1) -> bytes:
        raise AssertionError("stdin was not expected")


class _PoisonEnvironment(Mapping[str, str]):
    def __getitem__(self, _key: str) -> str:
        raise AssertionError("environment was accessed")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("environment was enumerated")

    def __len__(self) -> int:
        raise AssertionError("environment length was accessed")


def _finder(name: str) -> str | None:
    assert name == "claude"
    return "/opt/claude"


def _invoke(
    argv: list[str],
    stdin: io.BytesIO,
    backend: ProcessBackend,
    *,
    environment: Mapping[str, str] | None = None,
    finder: Callable[[str], str | None] = _finder,
    progress: Callable[[ReviewProgress], None] | None = None,
) -> tuple[int, dict[str, object], str]:
    from devtools.claude_qc.cli import run_cli

    stdout = io.StringIO()
    exit_code = run_cli(
        argv,
        stdin,
        stdout,
        backend,
        (
            {"HOME": "/home/tester", "PATH": "/usr/bin"}
            if environment is None
            else environment
        ),
        finder=finder,
        platform="linux",
        progress=progress,
    )
    text = stdout.getvalue()
    return exit_code, json.loads(text), text


def test_status_runs_only_subscription_preflight_without_reading_stdin() -> None:
    backend = _WorkflowBackend()

    exit_code, envelope, _ = _invoke(["status"], _NoRead(), backend)

    assert exit_code == 0
    assert envelope["status"] == "complete"
    assert envelope["category"] == "status"
    assert envelope["claude_code_version"] == "2.1.252"
    assert envelope["details"] == {"subscription_type": "pro"}
    assert len(backend.requests) == 3


def test_preview_is_exact_and_performs_no_claude_or_environment_access() -> None:
    raw = _packet_raw()
    canonical = canonical_packet_bytes(parse_packet(raw))

    exit_code, envelope, _ = _invoke(
        ["preview"],
        io.BytesIO(raw),
        _ForbiddenBackend(),
        environment=_PoisonEnvironment(),
        finder=lambda name: (_ for _ in ()).throw(AssertionError("finder called")),
    )

    details = envelope["details"]
    assert isinstance(details, dict)
    assert exit_code == 0
    assert details["canonical_packet"] == canonical.decode()
    assert details["digest"] == packet_digest(canonical)
    assert details["byte_size"] == len(canonical)
    assert "leave this machine" in str(details["outbound_data_warning"])


def test_review_rejects_digest_mismatch_before_preflight() -> None:
    exit_code, envelope, _ = _invoke(
        ["review", "--approved-digest", "0" * 64],
        io.BytesIO(_packet_raw()),
        _ForbiddenBackend(),
    )

    assert exit_code == 2
    assert envelope["status"] == "incomplete"
    assert envelope["category"] == "packet"


def test_review_handler_traverses_preflight_process_and_event_validation() -> None:
    backend = _WorkflowBackend()
    canonical = canonical_packet_bytes(parse_packet(_packet_raw()))

    exit_code, envelope, _ = _invoke(
        ["review", "--approved-digest", packet_digest(canonical)],
        io.BytesIO(_packet_raw()),
        backend,
    )

    assert exit_code == 0
    assert envelope["category"] == "review"
    assert envelope["findings"] == [_finding()]
    assert len(backend.requests) == 4
    assert backend.requests[-1].stdin == canonical


def test_review_progress_is_separate_from_final_json_envelope() -> None:
    backend = _WorkflowBackend()
    canonical = canonical_packet_bytes(parse_packet(_packet_raw()))
    progress: list[ReviewProgress] = []

    exit_code, envelope, text = _invoke(
        ["review", "--approved-digest", packet_digest(canonical)],
        io.BytesIO(_packet_raw()),
        backend,
        progress=progress.append,
    )

    assert exit_code == 0
    assert envelope["category"] == "review"
    assert text.count("\n") == 1
    assert progress == [
        ReviewProgress.STARTING,
        ReviewProgress.FINAL_RESPONSE,
    ]


def test_unknown_command_returns_one_usage_envelope_and_exit_two() -> None:
    exit_code, envelope, text = _invoke(
        ["unknown"],
        io.BytesIO(b"SYNTHETIC_SECRET_DO_NOT_EXPOSE_6B2F"),
        _ForbiddenBackend(),
    )

    assert exit_code == 2
    assert envelope["category"] == "usage"
    assert text.count("\n") == 1
    assert "SYNTHETIC_SECRET_DO_NOT_EXPOSE_6B2F" not in text


def test_direct_keyboard_interrupt_returns_incomplete_exit_130() -> None:
    exit_code, envelope, _ = _invoke(
        ["status"],
        _NoRead(),
        _InterruptBackend(),
    )

    assert exit_code == 130
    assert envelope["status"] == "incomplete"
    assert envelope["category"] == "interrupted"


def test_deep_packet_json_returns_versioned_packet_failure() -> None:
    exit_code, envelope, text = _invoke(
        ["preview"],
        io.BytesIO(_DEEPLY_NESTED_JSON),
        _ForbiddenBackend(),
    )

    assert exit_code == 2
    assert envelope["category"] == "packet"
    assert text.count("\n") == 1


def test_deep_auth_json_returns_versioned_preflight_failure() -> None:
    exit_code, envelope, text = _invoke(
        ["status"],
        _NoRead(),
        _DeepAuthBackend(),
    )

    assert exit_code == 3
    assert envelope["category"] == "subscription_unavailable"
    assert text.count("\n") == 1


def test_deep_output_json_returns_versioned_invalid_result() -> None:
    canonical = canonical_packet_bytes(parse_packet(_packet_raw()))

    exit_code, envelope, text = _invoke(
        ["review", "--approved-digest", packet_digest(canonical)],
        io.BytesIO(_packet_raw()),
        _DeepOutputBackend(),
    )

    assert exit_code == 5
    assert envelope["category"] == "invalid_result"
    assert text.count("\n") == 1


@pytest.mark.parametrize(
    "preflight_category, envelope_category",
    [
        (PreflightCategory.UNAVAILABLE, "claude_unavailable"),
        (PreflightCategory.INCOMPATIBLE, "claude_incompatible"),
        (PreflightCategory.SUBSCRIPTION, "subscription_unavailable"),
    ],
)
def test_preflight_failures_map_to_exact_sanitized_exit_three(
    monkeypatch: pytest.MonkeyPatch,
    preflight_category: PreflightCategory,
    envelope_category: str,
) -> None:
    from devtools.claude_qc import cli

    secret = "SYNTHETIC_PREFLIGHT_SECRET_6B2F"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise PreflightError(preflight_category, secret)

    monkeypatch.setattr(cli, "preflight_claude_code", fail)

    exit_code, envelope, text = _invoke(
        ["status"],
        _NoRead(),
        _ForbiddenBackend(),
    )

    assert exit_code == 3
    assert envelope["status"] == "incomplete"
    assert envelope["category"] == envelope_category
    assert secret not in text


@pytest.mark.parametrize(
    "review_category, exit_code, envelope_category",
    [
        (ReviewRunCategory.PROVIDER, 4, "provider"),
        (ReviewRunCategory.DEADLINE, 4, "deadline"),
        (ReviewRunCategory.UNSAFE, 5, "unsafe"),
        (ReviewRunCategory.INVALID, 5, "invalid_result"),
    ],
)
def test_review_failures_map_to_exact_sanitized_protocol(
    monkeypatch: pytest.MonkeyPatch,
    review_category: ReviewRunCategory,
    exit_code: int,
    envelope_category: str,
) -> None:
    from devtools.claude_qc import cli

    secret = "SYNTHETIC_PROVIDER_SECRET_91C4"
    canonical = canonical_packet_bytes(parse_packet(_packet_raw()))

    def installed(*_args: object, **_kwargs: object) -> ClaudeCodeInstallation:
        return ClaudeCodeInstallation(Path("/opt/claude"), "2.1.252", "pro")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ReviewRunError(review_category, secret)

    monkeypatch.setattr(cli, "preflight_claude_code", installed)
    monkeypatch.setattr(cli, "run_review", fail)

    actual_exit, envelope, text = _invoke(
        ["review", "--approved-digest", packet_digest(canonical)],
        io.BytesIO(_packet_raw()),
        _ForbiddenBackend(),
    )

    assert actual_exit == exit_code
    assert envelope["status"] == "incomplete"
    assert envelope["category"] == envelope_category
    assert secret not in text


def test_unsafe_review_failure_exposes_only_static_reason_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devtools.claude_qc import cli

    secret = "SYNTHETIC_PROVIDER_SECRET_3A7C"
    canonical = canonical_packet_bytes(parse_packet(_packet_raw()))

    def installed(*_args: object, **_kwargs: object) -> ClaudeCodeInstallation:
        return ClaudeCodeInstallation(Path("/opt/claude"), "2.1.252", "pro")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ReviewRunError(
            ReviewRunCategory.UNSAFE,
            secret,
            reason=ReviewRunReason.UNEXPECTED_LOCAL_ARTIFACT,
        )

    monkeypatch.setattr(cli, "preflight_claude_code", installed)
    monkeypatch.setattr(cli, "run_review", fail)

    actual_exit, envelope, text = _invoke(
        ["review", "--approved-digest", packet_digest(canonical)],
        io.BytesIO(_packet_raw()),
        _ForbiddenBackend(),
    )

    assert actual_exit == 5
    assert envelope["category"] == "unsafe"
    assert envelope["details"] == {
        "reason_code": "unexpected_local_artifact",
        "stage": "starting",
    }
    assert secret not in text


def test_invalid_review_failure_reports_only_static_validation_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devtools.claude_qc import cli

    secret = "SYNTHETIC_PROVIDER_SECRET_07FD"
    validation = "Claude Code reported an unexpected review turn count."
    canonical = canonical_packet_bytes(parse_packet(_packet_raw()))

    def installed(*_args: object, **_kwargs: object) -> ClaudeCodeInstallation:
        return ClaudeCodeInstallation(Path("/opt/claude"), "2.1.252", "pro")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ReviewRunError(
            ReviewRunCategory.INVALID,
            secret,
            stage=ReviewStage.FINAL_RESPONSE,
            validation_message=validation,
        )

    monkeypatch.setattr(cli, "preflight_claude_code", installed)
    monkeypatch.setattr(cli, "run_review", fail)

    actual_exit, envelope, text = _invoke(
        ["review", "--approved-digest", packet_digest(canonical)],
        io.BytesIO(_packet_raw()),
        _ForbiddenBackend(),
    )

    assert actual_exit == 5
    assert envelope["category"] == "invalid_result"
    assert envelope["details"] == {
        "stage": "final_response",
        "validation": validation,
    }
    assert secret not in text
