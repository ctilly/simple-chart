from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from simplechart.claude_qc.findings import (
    Finding,
    FindingValidationError,
    parse_findings,
)
from simplechart.claude_qc.packet import MODEL


_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


class EventValidationCategory(StrEnum):
    MALFORMED = "malformed"
    UNSAFE = "unsafe"
    MODEL = "model"
    PROVIDER = "provider"


class EventValidationError(RuntimeError):
    def __init__(self, category: EventValidationCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ReviewResult:
    claude_code_version: str
    model: str
    retry_count: int
    duration_ms: int
    findings: tuple[Finding, ...]


class _DuplicateEventFieldError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateEventFieldError
        result[key] = value
    return result


def _decode_events(raw: bytes) -> tuple[dict[str, object], ...]:
    decoded: str | None = None
    decode_failed = False
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
    if decode_failed or decoded is None or not decoded.strip():
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code returned an invalid event stream.",
        )

    events: list[dict[str, object]] = []
    parse_failed = False
    for line in decoded.splitlines():
        if not line.strip():
            continue
        value: object | None = None
        try:
            value = json.loads(line, object_pairs_hook=_strict_object)
        except (
            json.JSONDecodeError,
            _DuplicateEventFieldError,
            RecursionError,
        ):
            parse_failed = True
        if not isinstance(value, dict):
            parse_failed = True
            break
        events.append(value)
    if parse_failed or not events:
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code returned an invalid event stream.",
        )
    return tuple(events)


def _require_safe_initialization(
    event: dict[str, object],
    *,
    expected_cwd: Path,
    expected_version: str,
) -> bool:
    if (
        event.get("apiKeySource") not in {"none", "oauth"}
        or event.get("cwd") != str(expected_cwd)
        or event.get("claude_code_version") != expected_version
        or event.get("permissionMode") != "dontAsk"
    ):
        raise EventValidationError(
            EventValidationCategory.UNSAFE,
            "Claude Code initialization did not match the isolated review.",
        )
    for field in ("mcp_servers", "plugins", "skills", "slash_commands"):
        if event.get(field) != []:
            raise EventValidationError(
                EventValidationCategory.UNSAFE,
                "Claude Code initialized an unauthorized capability.",
            )
    tools = event.get("tools")
    if tools not in ([], [_STRUCTURED_OUTPUT_TOOL]):
        raise EventValidationError(
            EventValidationCategory.UNSAFE,
            "Claude Code initialized an unauthorized capability.",
        )
    agents = event.get("agents")
    if (
        not isinstance(agents, list)
        or any(not isinstance(agent, str) for agent in agents)
        or (tools == [] and agents)
    ):
        raise EventValidationError(
            EventValidationCategory.UNSAFE,
            "Claude Code initialized an unauthorized capability.",
        )
    if event.get("model") != MODEL:
        raise EventValidationError(
            EventValidationCategory.MODEL,
            "Claude Code initialized an unexpected model.",
        )
    return tools == [_STRUCTURED_OUTPUT_TOOL]


def _validate_assistant(
    event: dict[str, object],
    *,
    structured_output_enabled: bool,
) -> tuple[str, dict[str, object]] | None:
    message = event.get("message")
    if (
        event.get("error") is not None
        or event.get("parent_tool_use_id") not in (None,)
        or not isinstance(message, dict)
    ):
        raise EventValidationError(
            EventValidationCategory.PROVIDER,
            "Claude Code returned an incomplete assistant event.",
        )
    if message.get("model") != MODEL:
        raise EventValidationError(
            EventValidationCategory.MODEL,
            "Claude Code returned output from an unexpected model.",
        )
    content = message.get("content")
    if not isinstance(content, list):
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code returned a malformed assistant event.",
        )
    inert_block_types = {"text", "thinking", "redacted_thinking"}
    structured_call: tuple[str, dict[str, object]] | None = None
    for block in content:
        if not isinstance(block, dict):
            raise EventValidationError(
                EventValidationCategory.UNSAFE,
                "Claude Code attempted to use an unauthorized capability.",
            )
        if block.get("type") in inert_block_types:
            continue
        tool_id = block.get("id")
        tool_input = block.get("input")
        if (
            not structured_output_enabled
            or block.get("type") != "tool_use"
            or block.get("name") != _STRUCTURED_OUTPUT_TOOL
            or not isinstance(tool_id, str)
            or not tool_id
            or not isinstance(tool_input, dict)
            or structured_call is not None
        ):
            raise EventValidationError(
                EventValidationCategory.UNSAFE,
                "Claude Code attempted to use an unauthorized capability.",
            )
        structured_call = (tool_id, tool_input)
    stop_reason = message.get("stop_reason")
    if (
        message.get("stop_details") is not None
        or (
            structured_call is None
            and stop_reason not in (None, "end_turn")
        )
        or (
            structured_call is not None
            and stop_reason not in (None, "tool_use")
        )
    ):
        raise EventValidationError(
            EventValidationCategory.PROVIDER,
            "Claude Code reached its output limit.",
        )
    return structured_call


def _validate_structured_tool_result(
    event: dict[str, object],
    expected_tool_id: str,
) -> None:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or len(content) != 1:
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code returned an invalid structured-output result.",
        )
    block = content[0]
    if (
        not isinstance(block, dict)
        or block.get("type") != "tool_result"
        or block.get("tool_use_id") != expected_tool_id
    ):
        raise EventValidationError(
            EventValidationCategory.UNSAFE,
            "Claude Code returned an unauthorized tool result.",
        )


def _validate_retry(event: dict[str, object]) -> None:
    attempt = event.get("attempt")
    max_retries = event.get("max_retries")
    retry_delay_ms = event.get("retry_delay_ms")
    error_status = event.get("error_status")
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt < 1
        or not isinstance(max_retries, int)
        or isinstance(max_retries, bool)
        or max_retries < attempt
        or not isinstance(retry_delay_ms, int)
        or isinstance(retry_delay_ms, bool)
        or retry_delay_ms < 0
        or not isinstance(error_status, int)
        or isinstance(error_status, bool)
        or not isinstance(event.get("error"), str)
    ):
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code returned invalid retry metadata.",
        )


def _parse_terminal(
    event: dict[str, object],
    *,
    structured_output_enabled: bool,
    structured_output: dict[str, object] | None,
    tool_result_count: int,
) -> tuple[int, tuple[Finding, ...]]:
    expected_stop_reason = "tool_use" if structured_output_enabled else "end_turn"
    expected_turns = 2 if structured_output_enabled else 1
    if (
        event.get("subtype") != "success"
        or event.get("is_error") is not False
        or event.get("stop_reason") != expected_stop_reason
        or event.get("permission_denials") != []
        or event.get("num_turns") != expected_turns
        or (
            structured_output_enabled
            and (
                structured_output is None
                or tool_result_count != 1
                or event.get("structured_output") != structured_output
            )
        )
        or (not structured_output_enabled and tool_result_count != 0)
    ):
        raise EventValidationError(
            EventValidationCategory.PROVIDER,
            "Claude Code did not complete the review safely.",
        )
    model_usage = event.get("modelUsage")
    if not isinstance(model_usage, dict) or set(model_usage) != {MODEL}:
        raise EventValidationError(
            EventValidationCategory.MODEL,
            "Claude Code reported usage by an unexpected model.",
        )
    duration_ms = event.get("duration_ms")
    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms < 0
    ):
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code returned invalid review metadata.",
        )

    findings: tuple[Finding, ...] | None = None
    findings_failed = False
    try:
        findings = parse_findings(event.get("structured_output"))
    except FindingValidationError:
        findings_failed = True
    if findings_failed or findings is None:
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code returned invalid structured findings.",
        )
    return duration_ms, findings


def _parse_event_stream(
    raw: bytes,
    *,
    expected_cwd: Path,
    expected_version: str,
) -> ReviewResult:
    events = _decode_events(raw)
    initialization_count = 0
    assistant_count = 0
    retry_count = 0
    terminal: dict[str, object] | None = None
    initialized = False
    structured_output_enabled = False
    structured_call: tuple[str, dict[str, object]] | None = None
    tool_result_count = 0

    for index, event in enumerate(events):
        event_type = event.get("type")
        subtype = event.get("subtype")
        if event_type == "system" and subtype == "init":
            initialization_count += 1
            if initialized or index != 0:
                raise EventValidationError(
                    EventValidationCategory.MALFORMED,
                    "Claude Code returned duplicate initialization.",
                )
            structured_output_enabled = _require_safe_initialization(
                event,
                expected_cwd=expected_cwd,
                expected_version=expected_version,
            )
            initialized = True
            continue
        if not initialized:
            raise EventValidationError(
                EventValidationCategory.MALFORMED,
                "Claude Code omitted initialization.",
            )
        if terminal is not None:
            if event_type == "prompt_suggestion" and isinstance(
                event.get("suggestion"),
                str,
            ):
                continue
            raise EventValidationError(
                EventValidationCategory.MALFORMED,
                "Claude Code returned events after the terminal result.",
            )
        if event_type == "system" and subtype == "api_retry":
            _validate_retry(event)
            retry_count += 1
            continue
        if event_type == "system" and subtype == "thinking_tokens":
            continue
        if event_type == "assistant":
            candidate = _validate_assistant(
                event,
                structured_output_enabled=structured_output_enabled,
            )
            if candidate is not None:
                if structured_call is not None:
                    raise EventValidationError(
                        EventValidationCategory.UNSAFE,
                        "Claude Code attempted duplicate structured output.",
                    )
                structured_call = candidate
            assistant_count += 1
            continue
        if event_type == "user":
            if structured_call is None or tool_result_count != 0:
                raise EventValidationError(
                    EventValidationCategory.UNSAFE,
                    "Claude Code returned an unauthorized tool result.",
                )
            _validate_structured_tool_result(event, structured_call[0])
            tool_result_count += 1
            continue
        if event_type == "rate_limit_event":
            continue
        if event_type == "result":
            terminal = event
            continue
        raise EventValidationError(
            EventValidationCategory.PROVIDER,
            "Claude Code returned an unsupported event.",
        )

    if initialization_count != 1 or assistant_count < 1 or terminal is None:
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code returned an incomplete event stream.",
        )
    if structured_output_enabled != (structured_call is not None):
        raise EventValidationError(
            EventValidationCategory.MALFORMED,
            "Claude Code omitted required structured output.",
        )
    duration_ms, findings = _parse_terminal(
        terminal,
        structured_output_enabled=structured_output_enabled,
        structured_output=(structured_call[1] if structured_call is not None else None),
        tool_result_count=tool_result_count,
    )
    return ReviewResult(
        claude_code_version=expected_version,
        model=MODEL,
        retry_count=retry_count,
        duration_ms=duration_ms,
        findings=findings,
    )


def _raise_event_error(
    category: EventValidationCategory,
    message: str,
) -> NoReturn:
    raise EventValidationError(category, message)


def parse_event_stream(
    raw: bytes,
    *,
    expected_cwd: Path,
    expected_version: str,
) -> ReviewResult:
    result: ReviewResult | None = None
    failure_category: EventValidationCategory | None = None
    failure_message = ""
    try:
        result = _parse_event_stream(
            raw,
            expected_cwd=expected_cwd,
            expected_version=expected_version,
        )
    except EventValidationError as error:
        failure_category = error.category
        failure_message = str(error)
    del raw, expected_cwd, expected_version
    if failure_category is not None:
        _raise_event_error(failure_category, failure_message)
    if result is None:
        _raise_event_error(
            EventValidationCategory.MALFORMED,
            "Claude Code returned an invalid event stream.",
        )
    return result
