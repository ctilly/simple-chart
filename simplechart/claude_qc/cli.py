from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO, Final, TextIO

from simplechart.claude_qc.events import ReviewResult
from simplechart.claude_qc.findings import Finding
from simplechart.claude_qc.packet import (
    MODEL,
    PacketPreview,
    PacketValidationError,
    build_preview,
    canonical_packet_bytes,
    parse_packet,
    require_approved_digest,
)
from simplechart.claude_qc.preflight import (
    PreflightCategory,
    PreflightError,
    preflight_claude_code,
)
from simplechart.claude_qc.process import ProcessBackend, SubprocessBackend
from simplechart.claude_qc.runner import (
    ReviewRunCategory,
    ReviewRunError,
    run_review,
)


PROTOCOL_VERSION = "1"
_USAGE_MESSAGE: Final = (
    "Use status, preview, or review --approved-digest DIGEST."
)


def _finding_value(finding: Finding) -> dict[str, str]:
    return {
        "finding_id": finding.finding_id,
        "severity": finding.severity.value,
        "claim": finding.claim,
        "evidence": finding.evidence,
        "impact": finding.impact,
        "falsification_check": finding.falsification_check,
        "suggested_disposition": finding.suggested_disposition.value,
        "confidence": finding.confidence.value,
    }


def _envelope(
    *,
    status: str,
    category: str,
    message: str,
    claude_code_version: str | None = None,
    retry_count: int = 0,
    duration_ms: int = 0,
    findings: tuple[Finding, ...] = (),
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": status,
        "category": category,
        "message": message,
        "claude_code_version": claude_code_version,
        "model": MODEL,
        "retry_count": retry_count,
        "duration_ms": duration_ms,
        "findings": [_finding_value(finding) for finding in findings],
        "details": dict(details or {}),
    }


def _preview_details(preview: PacketPreview) -> dict[str, object]:
    return {
        "canonical_packet": preview.canonical_packet,
        "digest": preview.digest,
        "byte_size": preview.byte_size,
        "model": preview.model,
        "effort": preview.effort,
        "categories": list(preview.categories),
        "retention_notice": preview.retention_notice,
        "outbound_data_warning": preview.outbound_data_warning,
    }


def _write_envelope(stdout: TextIO, envelope: Mapping[str, object]) -> None:
    stdout.write(json.dumps(envelope, separators=(",", ":"), sort_keys=True))
    stdout.write("\n")


def _parse_operation(argv: Sequence[str]) -> tuple[str, str | None] | None:
    if list(argv) == ["status"]:
        return "status", None
    if list(argv) == ["preview"]:
        return "preview", None
    if len(argv) == 3 and argv[0] == "review" and argv[1] == "--approved-digest":
        return "review", argv[2]
    return None


def _preflight_failure(error: PreflightError) -> dict[str, object]:
    categories = {
        PreflightCategory.UNAVAILABLE: "claude_unavailable",
        PreflightCategory.INCOMPATIBLE: "claude_incompatible",
        PreflightCategory.SUBSCRIPTION: "subscription_unavailable",
    }
    return _envelope(
        status="incomplete",
        category=categories[error.category],
        message="Claude Code preflight did not pass.",
    )


def _review_failure(error: ReviewRunError) -> tuple[int, dict[str, object]]:
    if error.category is ReviewRunCategory.DEADLINE:
        category = "deadline"
        exit_code = 4
    elif error.category is ReviewRunCategory.PROVIDER:
        category = "provider"
        exit_code = 4
    elif error.category is ReviewRunCategory.UNSAFE:
        category = "unsafe"
        exit_code = 5
    else:
        category = "invalid_result"
        exit_code = 5
    return exit_code, _envelope(
        status="incomplete",
        category=category,
        message="The Claude review did not complete safely.",
    )


def _review_success(result: ReviewResult) -> dict[str, object]:
    return _envelope(
        status="complete",
        category="review",
        message="The Claude review is complete.",
        claude_code_version=result.claude_code_version,
        retry_count=result.retry_count,
        duration_ms=result.duration_ms,
        findings=result.findings,
        details={"finding_count": len(result.findings)},
    )


def _dispatch(
    operation: str,
    approved_digest: str | None,
    stdin: BinaryIO,
    backend: ProcessBackend,
    parent_environment: Mapping[str, str],
    *,
    finder: Callable[[str], str | None],
    platform: str,
) -> tuple[int, dict[str, object]]:
    if operation == "preview":
        packet = parse_packet(stdin.read())
        preview = build_preview(packet)
        return 0, _envelope(
            status="complete",
            category="preview",
            message="The review packet preview is complete.",
            details=_preview_details(preview),
        )

    if operation == "status":
        installation = preflight_claude_code(
            backend,
            parent_environment,
            finder=finder,
            platform=platform,
        )
        return 0, _envelope(
            status="complete",
            category="status",
            message="Claude Code subscription preflight passed.",
            claude_code_version=installation.version,
            details={"subscription_type": installation.subscription_type},
        )

    packet = parse_packet(stdin.read())
    canonical = canonical_packet_bytes(packet)
    if approved_digest is None:
        raise PacketValidationError("The approved packet digest is missing.")
    require_approved_digest(canonical, approved_digest)
    installation = preflight_claude_code(
        backend,
        parent_environment,
        finder=finder,
        platform=platform,
    )
    result = run_review(
        installation,
        canonical,
        backend,
        parent_environment,
        platform=platform,
    )
    return 0, _review_success(result)


def run_cli(
    argv: Sequence[str],
    stdin: BinaryIO,
    stdout: TextIO,
    backend: ProcessBackend,
    parent_environment: Mapping[str, str],
    *,
    finder: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
) -> int:
    operation = _parse_operation(argv)
    if operation is None:
        _write_envelope(
            stdout,
            _envelope(
                status="incomplete",
                category="usage",
                message=_USAGE_MESSAGE,
            ),
        )
        return 2

    exit_code = 0
    envelope: dict[str, object] | None = None
    try:
        exit_code, envelope = _dispatch(
            operation[0],
            operation[1],
            stdin,
            backend,
            parent_environment,
            finder=finder,
            platform=platform,
        )
    except PacketValidationError:
        exit_code = 2
        envelope = _envelope(
            status="incomplete",
            category="packet",
            message="The review packet or approved digest is invalid.",
        )
    except PreflightError as error:
        exit_code = 3
        envelope = _preflight_failure(error)
    except ReviewRunError as error:
        exit_code, envelope = _review_failure(error)
    except KeyboardInterrupt:
        exit_code = 130
        envelope = _envelope(
            status="incomplete",
            category="interrupted",
            message="The Claude QC operation was interrupted.",
        )
    if envelope is None:
        exit_code = 5
        envelope = _envelope(
            status="incomplete",
            category="invalid_result",
            message="The Claude QC operation returned no result.",
        )
    _write_envelope(stdout, envelope)
    return exit_code


def main() -> int:
    return run_cli(
        sys.argv[1:],
        sys.stdin.buffer,
        sys.stdout,
        SubprocessBackend(),
        os.environ,
    )


if __name__ == "__main__":
    raise SystemExit(main())
