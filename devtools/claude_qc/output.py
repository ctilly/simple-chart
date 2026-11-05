from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from devtools.claude_qc.findings import (
    Finding,
    FindingValidationError,
    parse_findings,
)
from devtools.claude_qc.packet import MODEL


class OutputValidationCategory(StrEnum):
    MALFORMED = "malformed"
    UNSAFE = "unsafe"
    MODEL = "model"
    PROVIDER = "provider"


class OutputValidationError(RuntimeError):
    def __init__(
        self,
        category: OutputValidationCategory,
        message: str,
    ) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ReviewResult:
    claude_code_version: str
    model: str
    retry_count: int
    duration_ms: int
    findings: tuple[Finding, ...]


class _DuplicateOutputFieldError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateOutputFieldError
        result[key] = value
    return result


def _decode_output(raw: bytes) -> dict[str, object]:
    value: object | None = None
    failed = False
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateOutputFieldError,
        RecursionError,
    ):
        failed = True
    del raw
    if failed or not isinstance(value, dict):
        raise OutputValidationError(
            OutputValidationCategory.MALFORMED,
            "Claude Code returned invalid JSON.",
        )
    return value


def _parse_review_output(raw: bytes, *, expected_version: str) -> ReviewResult:
    value = _decode_output(raw)
    if value.get("type") != "result" or value.get("subtype") != "success":
        raise OutputValidationError(
            OutputValidationCategory.PROVIDER,
            "Claude Code reported an unsuccessful review.",
        )
    if value.get("is_error") is not False:
        raise OutputValidationError(
            OutputValidationCategory.PROVIDER,
            "Claude Code marked the review as an error.",
        )
    if value.get("permission_denials") != []:
        raise OutputValidationError(
            OutputValidationCategory.UNSAFE,
            "Claude Code reported a denied capability request.",
        )

    model_usage = value.get("modelUsage")
    if not isinstance(model_usage, dict) or set(model_usage) != {MODEL}:
        raise OutputValidationError(
            OutputValidationCategory.MODEL,
            "Claude Code reported usage by an unexpected model.",
        )
    requested_usage = model_usage[MODEL]
    output_tokens = (
        requested_usage.get("outputTokens")
        if isinstance(requested_usage, dict)
        else None
    )
    if (
        not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 1
    ):
        raise OutputValidationError(
            OutputValidationCategory.MODEL,
            "Claude Code did not report output from the requested model.",
        )
    duration_ms = value.get("duration_ms")
    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms < 0
    ):
        raise OutputValidationError(
            OutputValidationCategory.MALFORMED,
            "Claude Code returned invalid review metadata.",
        )

    findings: tuple[Finding, ...] | None = None
    findings_failed = False
    try:
        findings = parse_findings(value.get("structured_output"))
    except FindingValidationError:
        findings_failed = True
    del value
    if findings_failed or findings is None:
        raise OutputValidationError(
            OutputValidationCategory.MALFORMED,
            "Claude Code returned invalid structured findings.",
        )
    return ReviewResult(
        claude_code_version=expected_version,
        model=MODEL,
        retry_count=0,
        duration_ms=duration_ms,
        findings=findings,
    )


def _raise_output_error(
    category: OutputValidationCategory,
    message: str,
) -> NoReturn:
    raise OutputValidationError(category, message)


def parse_review_output(raw: bytes, *, expected_version: str) -> ReviewResult:
    result: ReviewResult | None = None
    failure_category: OutputValidationCategory | None = None
    failure_message = ""
    try:
        result = _parse_review_output(raw, expected_version=expected_version)
    except OutputValidationError as error:
        failure_category = error.category
        failure_message = str(error)
    del raw, expected_version
    if failure_category is not None:
        _raise_output_error(failure_category, failure_message)
    if result is None:
        _raise_output_error(
            OutputValidationCategory.MALFORMED,
            "Claude Code returned invalid JSON.",
        )
    return result
