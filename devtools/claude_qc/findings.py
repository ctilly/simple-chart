from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NoReturn


MAX_FINDINGS = 15


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SuggestedDisposition(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"
    NEEDS_USER_DECISION = "needs_user_decision"
    DUPLICATE = "duplicate"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Finding:
    finding_id: str
    severity: Severity
    claim: str
    evidence: str
    impact: str
    falsification_check: str
    suggested_disposition: SuggestedDisposition
    confidence: Confidence


_FINDING_FIELDS: Final = frozenset(
    {
        "finding_id",
        "severity",
        "claim",
        "evidence",
        "impact",
        "falsification_check",
        "suggested_disposition",
        "confidence",
    }
)

FINDINGS_JSON_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": MAX_FINDINGS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "finding_id": {"type": "string", "minLength": 1},
                    "severity": {"enum": [item.value for item in Severity]},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "impact": {"type": "string"},
                    "falsification_check": {"type": "string"},
                    "suggested_disposition": {
                        "enum": [item.value for item in SuggestedDisposition]
                    },
                    "confidence": {"enum": [item.value for item in Confidence]},
                },
                "required": sorted(_FINDING_FIELDS),
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def _parse_enum[EnumType: StrEnum](
    enum_type: type[EnumType],
    value: str,
) -> EnumType:
    result: EnumType | None = None
    try:
        result = enum_type(value)
    except ValueError:
        pass
    if result is None:
        raise FindingValidationError("A finding enum value is invalid.")
    return result


def _parse_finding(value: object) -> Finding:
    if not isinstance(value, dict) or set(value) != _FINDING_FIELDS:
        raise FindingValidationError("A finding has invalid fields.")
    strings: dict[str, str] = {}
    for field in _FINDING_FIELDS:
        field_value = value[field]
        if not isinstance(field_value, str):
            raise FindingValidationError("Every finding field must be a string.")
        strings[field] = field_value
    if not strings["finding_id"]:
        raise FindingValidationError("A finding ID is empty.")
    return Finding(
        finding_id=strings["finding_id"],
        severity=_parse_enum(Severity, strings["severity"]),
        claim=strings["claim"],
        evidence=strings["evidence"],
        impact=strings["impact"],
        falsification_check=strings["falsification_check"],
        suggested_disposition=_parse_enum(
            SuggestedDisposition,
            strings["suggested_disposition"],
        ),
        confidence=_parse_enum(Confidence, strings["confidence"]),
    )


def _parse_findings(value: object) -> tuple[Finding, ...]:
    if not isinstance(value, dict) or set(value) != {"findings"}:
        raise FindingValidationError("The structured result has invalid fields.")
    finding_values = value["findings"]
    if not isinstance(finding_values, list):
        raise FindingValidationError("The findings value must be an array.")
    if len(finding_values) > MAX_FINDINGS:
        raise FindingValidationError("The findings result exceeds 15 items.")
    findings = tuple(_parse_finding(item) for item in finding_values)
    ids = {finding.finding_id for finding in findings}
    if len(ids) != len(findings):
        raise FindingValidationError("Finding IDs must be unique.")
    return findings


def _raise_finding_error(message: str) -> NoReturn:
    raise FindingValidationError(message)


def parse_findings(value: object) -> tuple[Finding, ...]:
    findings: tuple[Finding, ...] | None = None
    failure_message: str | None = None
    try:
        findings = _parse_findings(value)
    except FindingValidationError as error:
        failure_message = str(error)
    del value
    if failure_message is not None:
        _raise_finding_error(failure_message)
    if findings is None:
        _raise_finding_error("The structured findings are invalid.")
    return findings
