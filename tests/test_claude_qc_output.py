import json

import pytest

from devtools.claude_qc.findings import (
    Confidence,
    Finding,
    Severity,
    SuggestedDisposition,
)
from devtools.claude_qc.output import (
    OutputValidationCategory,
    OutputValidationError,
    parse_review_output,
)
from devtools.claude_qc.packet import MODEL


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


def _result(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1234,
        "num_turns": 2,
        "stop_reason": "tool_use",
        "permission_denials": [],
        "modelUsage": {MODEL: {"inputTokens": 10, "outputTokens": 20}},
        "structured_output": {"findings": [_finding()]},
    }
    value.update(changes)
    return value


def _raw(**changes: object) -> bytes:
    return json.dumps(_result(**changes), separators=(",", ":")).encode()


def test_review_output_returns_normalized_findings() -> None:
    result = parse_review_output(_raw(), expected_version="2.1.318")

    assert result.claude_code_version == "2.1.318"
    assert result.model == MODEL
    assert result.retry_count == 0
    assert result.duration_ms == 1234
    assert result.findings == (
        Finding(
            finding_id="CQ-001",
            severity=Severity.MEDIUM,
            claim="The criterion is incomplete.",
            evidence="It does not name the blue value.",
            impact="A different color could pass.",
            falsification_check="Assert the exact rendered color.",
            suggested_disposition=SuggestedDisposition.ACCEPT,
            confidence=Confidence.HIGH,
        ),
    )


@pytest.mark.parametrize(
    ("changes", "category", "message"),
    [
        (
            {"subtype": "error_during_execution", "is_error": True},
            OutputValidationCategory.PROVIDER,
            "Claude Code reported an unsuccessful review.",
        ),
        (
            {"permission_denials": [{"tool": "Read"}]},
            OutputValidationCategory.UNSAFE,
            "Claude Code reported a denied capability request.",
        ),
        (
            {"modelUsage": {"claude-sonnet-5": {}}},
            OutputValidationCategory.MODEL,
            "Claude Code reported usage by an unexpected model.",
        ),
        (
            {"structured_output": {"findings": [], "extra": True}},
            OutputValidationCategory.MALFORMED,
            "Claude Code returned invalid structured findings.",
        ),
    ],
)
def test_review_output_rejects_invalid_results(
    changes: dict[str, object],
    category: OutputValidationCategory,
    message: str,
) -> None:
    with pytest.raises(OutputValidationError) as caught:
        parse_review_output(_raw(**changes), expected_version="2.1.318")

    assert caught.value.category is category
    assert str(caught.value) == message


def test_review_output_ignores_unknown_and_record_only_fields() -> None:
    result = parse_review_output(
        _raw(
            stop_reason="unknown_future_value",
            num_turns="not-a-decision-field",
            canonicalModel="future-model-metadata",
            credential_expires_at="synthetic",
            future_field={"nested": True},
        ),
        expected_version="2.1.318",
    )

    assert result.findings[0].finding_id == "CQ-001"


@pytest.mark.parametrize(
    "model_usage",
    [
        {MODEL: {"outputTokens": 0}},
        {MODEL: {}},
        {MODEL: {"outputTokens": 1}, "auxiliary-model": {"outputTokens": 1}},
    ],
)
def test_review_output_requires_only_requested_model_with_output(
    model_usage: dict[str, object],
) -> None:
    with pytest.raises(OutputValidationError) as caught:
        parse_review_output(
            _raw(modelUsage=model_usage),
            expected_version="2.1.318",
        )

    assert caught.value.category is OutputValidationCategory.MODEL


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not-json",
        b'{"type":"result","type":"assistant"}',
        b'{}\n{}',
    ],
)
def test_review_output_rejects_malformed_json(raw: bytes) -> None:
    with pytest.raises(OutputValidationError) as caught:
        parse_review_output(raw, expected_version="2.1.318")

    assert caught.value.category is OutputValidationCategory.MALFORMED


def test_review_output_does_not_retain_raw_provider_content() -> None:
    secret = "SYNTHETIC_SECRET_DO_NOT_EXPOSE_73AB"

    with pytest.raises(OutputValidationError) as caught:
        parse_review_output(
            json.dumps({"type": "error", "message": secret}).encode(),
            expected_version="2.1.318",
        )

    assert secret not in str(caught.value)
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith("claude_qc/output.py"):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
