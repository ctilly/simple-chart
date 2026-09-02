from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn


MODEL = "claude-opus-5"
EFFORT = "high"
MAX_PACKET_BYTES = 128 * 1024
PACKET_SCHEMA_VERSION = "1"

PACKET_FIELDS = (
    "schema_version",
    "review_kind",
    "user_objective",
    "applicable_constraints",
    "specification",
    "architecture_summary",
    "acceptance_criteria",
    "test_matrix",
    "implementation_slices",
    "known_risks",
    "open_questions",
    "implementation_diff",
    "verification_summary",
)

_CORE_NONEMPTY_FIELDS = (
    "user_objective",
    "applicable_constraints",
    "specification",
    "architecture_summary",
    "acceptance_criteria",
    "test_matrix",
    "implementation_slices",
)


class ReviewKind(StrEnum):
    DESIGN = "design"
    IMPLEMENTATION = "implementation"


class PacketValidationError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class ReviewPacket:
    schema_version: str
    review_kind: ReviewKind
    user_objective: str
    applicable_constraints: str
    specification: str
    architecture_summary: str
    acceptance_criteria: str
    test_matrix: str
    implementation_slices: str
    known_risks: str
    open_questions: str
    implementation_diff: str
    verification_summary: str


@dataclass(frozen=True, repr=False)
class PacketPreview:
    canonical_packet: str
    digest: str
    byte_size: int
    model: str
    effort: str
    categories: tuple[str, ...]
    retention_notice: str
    outbound_data_warning: str


class _DuplicateKeyError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode_packet(raw: bytes) -> dict[str, object]:
    value: object | None = None
    invalid = False
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_strict_object)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKeyError,
        RecursionError,
    ):
        invalid = True
    if invalid:
        raise PacketValidationError("The review packet is not valid JSON.")
    if not isinstance(value, dict):
        raise PacketValidationError("The review packet must be a JSON object.")
    return value


def _require_string_fields(value: dict[str, object]) -> dict[str, str]:
    if set(value) != set(PACKET_FIELDS):
        raise PacketValidationError("The review packet fields are invalid.")
    strings: dict[str, str] = {}
    for field in PACKET_FIELDS:
        field_value = value[field]
        if not isinstance(field_value, str):
            raise PacketValidationError("Every review packet field must be a string.")
        strings[field] = field_value
    return strings


def _parse_packet(raw: bytes) -> ReviewPacket:
    values = _require_string_fields(_decode_packet(raw))
    if values["schema_version"] != PACKET_SCHEMA_VERSION:
        raise PacketValidationError("The review packet schema version is unsupported.")
    review_kind: ReviewKind | None = None
    try:
        review_kind = ReviewKind(values["review_kind"])
    except ValueError:
        pass
    if review_kind is None:
        raise PacketValidationError("The review kind is invalid.")
    if any(not values[field] for field in _CORE_NONEMPTY_FIELDS):
        raise PacketValidationError("Required review context is empty.")
    implementation_diff = values["implementation_diff"]
    verification_summary = values["verification_summary"]
    if review_kind is ReviewKind.DESIGN and (
        implementation_diff or verification_summary
    ):
        raise PacketValidationError("Design review cannot include implementation evidence.")
    if review_kind is ReviewKind.IMPLEMENTATION and (
        not implementation_diff or not verification_summary
    ):
        raise PacketValidationError("Implementation review evidence is incomplete.")
    packet = ReviewPacket(
        schema_version=values["schema_version"],
        review_kind=review_kind,
        user_objective=values["user_objective"],
        applicable_constraints=values["applicable_constraints"],
        specification=values["specification"],
        architecture_summary=values["architecture_summary"],
        acceptance_criteria=values["acceptance_criteria"],
        test_matrix=values["test_matrix"],
        implementation_slices=values["implementation_slices"],
        known_risks=values["known_risks"],
        open_questions=values["open_questions"],
        implementation_diff=implementation_diff,
        verification_summary=verification_summary,
    )
    canonical_packet_bytes(packet)
    return packet


def _raise_packet_error(message: str) -> NoReturn:
    raise PacketValidationError(message)


def parse_packet(raw: bytes) -> ReviewPacket:
    packet: ReviewPacket | None = None
    failure_message: str | None = None
    try:
        packet = _parse_packet(raw)
    except PacketValidationError as error:
        failure_message = str(error)
    del raw
    if failure_message is not None:
        _raise_packet_error(failure_message)
    if packet is None:
        _raise_packet_error("The review packet is invalid.")
    return packet


def _packet_mapping(packet: ReviewPacket) -> dict[str, str]:
    return {
        "schema_version": packet.schema_version,
        "review_kind": packet.review_kind.value,
        "user_objective": packet.user_objective,
        "applicable_constraints": packet.applicable_constraints,
        "specification": packet.specification,
        "architecture_summary": packet.architecture_summary,
        "acceptance_criteria": packet.acceptance_criteria,
        "test_matrix": packet.test_matrix,
        "implementation_slices": packet.implementation_slices,
        "known_risks": packet.known_risks,
        "open_questions": packet.open_questions,
        "implementation_diff": packet.implementation_diff,
        "verification_summary": packet.verification_summary,
    }


def canonical_packet_bytes(packet: ReviewPacket) -> bytes:
    text = json.dumps(
        _packet_mapping(packet),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    canonical: bytes | None = None
    try:
        canonical = text.encode("utf-8")
    except UnicodeEncodeError:
        pass
    if canonical is None:
        raise PacketValidationError("The review packet contains invalid Unicode.")
    if len(canonical) > MAX_PACKET_BYTES:
        raise PacketValidationError("The review packet exceeds 128 KiB.")
    return canonical


def packet_digest(canonical: bytes) -> str:
    return hashlib.sha256(canonical).hexdigest()


def build_preview(packet: ReviewPacket) -> PacketPreview:
    canonical = canonical_packet_bytes(packet)
    return PacketPreview(
        canonical_packet=canonical.decode("utf-8"),
        digest=packet_digest(canonical),
        byte_size=len(canonical),
        model=MODEL,
        effort=EFFORT,
        categories=tuple(sorted(PACKET_FIELDS)),
        retention_notice=(
            "For consumer Pro and Max accounts, Anthropic may retain submitted data "
            "for 30 days when model-improvement use is disabled. If it is enabled, "
            "data may be retained longer and used for model training."
        ),
        outbound_data_warning=(
            "The complete canonical packet will leave this machine and be sent to Anthropic."
        ),
    )


def require_approved_digest(canonical: bytes, approved_digest: str) -> None:
    actual_digest = packet_digest(canonical)
    if not hmac.compare_digest(actual_digest, approved_digest):
        raise PacketValidationError("The approved packet digest does not match.")
