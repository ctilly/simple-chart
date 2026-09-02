import json
from pathlib import Path

import pytest

from simplechart.claude_qc.events import (
    EventValidationCategory,
    EventValidationError,
    parse_event_stream,
)
from simplechart.claude_qc.packet import MODEL


def _finding() -> dict[str, str]:
    return {
        "finding_id": "CQ-001",
        "severity": "medium",
        "claim": "The criterion is incomplete.",
        "evidence": "It does not name the blue value.",
        "impact": "A different color could pass.",
        "falsification_check": "Assert the exact rendered color.",
        "suggested_disposition": "accept",
        "confidence": "high",
    }


def _init(cwd: str, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "system",
        "subtype": "init",
        "apiKeySource": "oauth",
        "claude_code_version": "2.1.252",
        "cwd": cwd,
        "tools": [],
        "mcp_servers": [],
        "model": MODEL,
        "permissionMode": "dontAsk",
        "slash_commands": [],
        "skills": [],
        "plugins": [],
        "agents": [],
    }
    value.update(changes)
    return value


def _assistant(**message_changes: object) -> dict[str, object]:
    message: dict[str, object] = {
        "id": "msg_synthetic",
        "content": [{"type": "text", "text": "Synthetic review complete."}],
        "model": MODEL,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    message.update(message_changes)
    return {"type": "assistant", "message": message, "error": None}


def _result(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1234,
        "num_turns": 1,
        "stop_reason": "end_turn",
        "permission_denials": [],
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {MODEL: {"inputTokens": 10, "outputTokens": 20}},
        "structured_output": {"findings": [_finding()]},
    }
    value.update(changes)
    return value


def _stream(*events: dict[str, object]) -> bytes:
    return b"\n".join(
        json.dumps(event, separators=(",", ":")).encode("utf-8")
        for event in events
    ) + b"\n"


def test_safe_event_stream_returns_normalized_findings_and_retries(
    tmp_path: Path,
) -> None:
    retry = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 10,
        "retry_delay_ms": 100,
        "error_status": 529,
        "error": "server_error",
    }

    result = parse_event_stream(
        _stream(_init(str(tmp_path)), retry, _assistant(), _result()),
        expected_cwd=tmp_path,
        expected_version="2.1.252",
    )

    assert result.claude_code_version == "2.1.252"
    assert result.model == MODEL
    assert result.retry_count == 1
    assert result.duration_ms == 1234
    assert result.findings[0].finding_id == "CQ-001"


def test_current_claude_code_structured_output_stream_is_accepted(
    tmp_path: Path,
) -> None:
    finding = _finding()
    structured_output = {"findings": [finding]}
    thinking = _assistant(
        content=[{"type": "thinking", "thinking": "Synthetic reasoning."}],
        stop_reason=None,
    )
    structured = _assistant(
        content=[
            {
                "type": "tool_use",
                "id": "toolu_synthetic",
                "name": "StructuredOutput",
                "input": structured_output,
            }
        ],
        stop_reason=None,
    )
    tool_result = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_synthetic",
                    "content": "Structured output provided successfully",
                }
            ]
        },
    }
    result_event = _result(
        num_turns=2,
        stop_reason="tool_use",
        structured_output=structured_output,
    )

    result = parse_event_stream(
        _stream(
            _init(
                str(tmp_path),
                apiKeySource="none",
                tools=["StructuredOutput"],
                agents=["general-purpose", "Explore", "Plan", "statusline-setup"],
            ),
            {"type": "system", "subtype": "thinking_tokens", "max_tokens": 1},
            thinking,
            structured,
            tool_result,
            {"type": "rate_limit_event", "rate_limit_info": {}},
            result_event,
        ),
        expected_cwd=tmp_path,
        expected_version="2.1.252",
    )

    assert result.findings[0].finding_id == "CQ-001"


def test_safe_prompt_suggestion_after_terminal_result_is_consumed(
    tmp_path: Path,
) -> None:
    trailing = {
        "type": "prompt_suggestion",
        "suggestion": "Review another synthetic criterion.",
    }

    result = parse_event_stream(
        _stream(_init(str(tmp_path)), _assistant(), _result(), trailing),
        expected_cwd=tmp_path,
        expected_version="2.1.252",
    )

    assert result.findings[0].finding_id == "CQ-001"


def test_event_stream_rejects_malformed_retry_metadata(tmp_path: Path) -> None:
    retry = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": "one",
        "max_retries": 10,
        "retry_delay_ms": 100,
        "error_status": 529,
        "error": "server_error",
    }

    with pytest.raises(EventValidationError) as caught:
        parse_event_stream(
            _stream(_init(str(tmp_path)), retry, _assistant(), _result()),
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )

    assert caught.value.category is EventValidationCategory.MALFORMED


@pytest.mark.parametrize(
    "change",
    [
        {"apiKeySource": "user"},
        {"tools": ["Read"]},
        {"mcp_servers": [{"name": "hostile", "status": "connected"}]},
        {"plugins": [{"name": "hostile", "path": "/tmp/hostile"}]},
        {"skills": ["hostile"]},
        {"slash_commands": ["/hostile"]},
        {"agents": ["hostile"]},
        {"permissionMode": "acceptEdits"},
    ],
)
def test_event_stream_rejects_nonempty_authority_surfaces(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    with pytest.raises(EventValidationError) as caught:
        parse_event_stream(
            _stream(_init(str(tmp_path), **change), _assistant(), _result()),
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )

    assert caught.value.category is EventValidationCategory.UNSAFE


def test_event_stream_rejects_tool_use_even_after_safe_initialization(
    tmp_path: Path,
) -> None:
    assistant = _assistant(
        content=[{"type": "tool_use", "name": "Read", "input": {}}]
    )

    with pytest.raises(EventValidationError) as caught:
        parse_event_stream(
            _stream(_init(str(tmp_path)), assistant, _result()),
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )

    assert caught.value.category is EventValidationCategory.UNSAFE


def test_event_stream_accepts_inert_thinking_blocks(tmp_path: Path) -> None:
    assistant = _assistant(
        content=[
            {"type": "thinking", "thinking": "Synthetic private reasoning."},
            {"type": "redacted_thinking", "data": "synthetic-redacted"},
            {"type": "text", "text": "Synthetic review complete."},
        ]
    )

    result = parse_event_stream(
        _stream(_init(str(tmp_path)), assistant, _result()),
        expected_cwd=tmp_path,
        expected_version="2.1.252",
    )

    assert result.findings[0].finding_id == "CQ-001"


def test_event_stream_rejects_refusal_stop_details(tmp_path: Path) -> None:
    assistant = _assistant(
        stop_details={"type": "refusal", "refusal": "Synthetic refusal."}
    )

    with pytest.raises(EventValidationError) as caught:
        parse_event_stream(
            _stream(_init(str(tmp_path)), assistant, _result()),
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )

    assert caught.value.category is EventValidationCategory.PROVIDER


def test_event_stream_rejects_unexpected_assistant_stop_reason(
    tmp_path: Path,
) -> None:
    assistant = _assistant(stop_reason="pause_turn")

    with pytest.raises(EventValidationError) as caught:
        parse_event_stream(
            _stream(_init(str(tmp_path)), assistant, _result()),
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )

    assert caught.value.category is EventValidationCategory.PROVIDER


@pytest.mark.parametrize(
    "events",
    [
        lambda cwd: (_assistant(), _result()),
        lambda cwd: (_init(cwd), _init(cwd), _assistant(), _result()),
        lambda cwd: (_init(cwd, model="claude-sonnet-5"), _assistant(), _result()),
        lambda cwd: (_init(cwd), _assistant(model="claude-sonnet-5"), _result()),
        lambda cwd: (
            _init(cwd),
            _assistant(),
            _result(modelUsage={"claude-sonnet-5": {}}),
        ),
    ],
)
def test_event_stream_requires_single_init_and_exact_model(
    tmp_path: Path,
    events: object,
) -> None:
    factory = events
    assert callable(factory)
    event_values = factory(str(tmp_path))

    with pytest.raises(EventValidationError):
        parse_event_stream(
            _stream(*event_values),
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )


@pytest.mark.parametrize(
    "terminal",
    [
        _result(subtype="error_during_execution", is_error=True),
        _result(stop_reason="max_tokens"),
        _result(permission_denials=[{"tool": "Read"}]),
        _result(structured_output={"findings": [], "extra": True}),
    ],
)
def test_event_stream_rejects_incomplete_or_invalid_terminal_results(
    tmp_path: Path,
    terminal: dict[str, object],
) -> None:
    with pytest.raises(EventValidationError):
        parse_event_stream(
            _stream(_init(str(tmp_path)), _assistant(), terminal),
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json\n",
        b'{"type":"system","type":"result"}\n',
        b"",
    ],
)
def test_event_stream_rejects_malformed_or_empty_output(raw: bytes, tmp_path: Path) -> None:
    with pytest.raises(EventValidationError) as caught:
        parse_event_stream(
            raw,
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_event_errors_do_not_retain_raw_provider_canaries(tmp_path: Path) -> None:
    secret = "SYNTHETIC_SECRET_DO_NOT_EXPOSE_6B2F"
    raw = _stream(_init(str(tmp_path)), {"type": "error", "message": secret})

    with pytest.raises(EventValidationError) as caught:
        parse_event_stream(
            raw,
            expected_cwd=tmp_path,
            expected_version="2.1.252",
        )

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value.args)
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith("claude_qc/events.py"):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
