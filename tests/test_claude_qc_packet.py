import builtins
import hashlib
import socket
import sqlite3
import subprocess

import pytest

from simplechart.claude_qc.packet import (
    MAX_PACKET_BYTES,
    PacketValidationError,
    ReviewKind,
    build_preview,
    canonical_packet_bytes,
    parse_packet,
    require_approved_digest,
)


def _packet(**changes: str) -> dict[str, str]:
    values = {
        "schema_version": "1",
        "review_kind": "design",
        "user_objective": "Review a synthetic design.",
        "applicable_constraints": "Use only supplied evidence.",
        "specification": "A fictional badge is blue.",
        "architecture_summary": "One inert component.",
        "acceptance_criteria": "The badge color is observable.",
        "test_matrix": "One deterministic color assertion.",
        "implementation_slices": "One rendering slice.",
        "known_risks": "",
        "open_questions": "",
        "implementation_diff": "",
        "verification_summary": "",
    }
    values.update(changes)
    return values


def _raw_packet(**changes: str) -> bytes:
    import json

    return json.dumps(_packet(**changes)).encode("utf-8")


def test_packet_canonical_bytes_match_the_approved_utf8_contract() -> None:
    packet = parse_packet(_raw_packet(user_objective="Review café."))

    canonical = canonical_packet_bytes(packet)

    assert canonical == (
        b'{"acceptance_criteria":"The badge color is observable.",'
        b'"applicable_constraints":"Use only supplied evidence.",'
        b'"architecture_summary":"One inert component.",'
        b'"implementation_diff":"","implementation_slices":"One rendering slice.",'
        b'"known_risks":"","open_questions":"","review_kind":"design",'
        b'"schema_version":"1","specification":"A fictional badge is blue.",'
        b'"test_matrix":"One deterministic color assertion.",'
        b'"user_objective":"Review caf\xc3\xa9.","verification_summary":""}'
    )


def test_packet_rejects_unknown_fields_before_any_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SYNTHETIC_SECRET_DO_NOT_EXPOSE_6B2F"
    values = _packet()
    values["credential"] = secret
    import json

    def unexpected_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("packet validation performed I/O")

    monkeypatch.setattr(builtins, "open", unexpected_io)
    monkeypatch.setattr(sqlite3, "connect", unexpected_io)
    monkeypatch.setattr(subprocess, "run", unexpected_io)
    monkeypatch.setattr(socket, "socket", unexpected_io)

    with pytest.raises(PacketValidationError) as caught:
        parse_packet(json.dumps(values).encode("utf-8"))

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"[]",
        b'{"schema_version":"1","schema_version":"1"}',
        b'"not-an-object"',
        b"\xff",
    ],
)
def test_packet_rejects_malformed_or_duplicate_json(raw: bytes) -> None:
    with pytest.raises(PacketValidationError):
        parse_packet(raw)


def test_packet_requires_exact_schema_and_string_fields() -> None:
    missing = _packet()
    del missing["user_objective"]

    with pytest.raises(PacketValidationError):
        parse_packet(__import__("json").dumps(missing).encode("utf-8"))
    with pytest.raises(PacketValidationError):
        parse_packet(_raw_packet(schema_version="2"))

    wrong_type_values: dict[str, object] = {}
    wrong_type_values.update(_packet())
    wrong_type_values["known_risks"] = []
    wrong_type = __import__("json").dumps(wrong_type_values).encode("utf-8")
    with pytest.raises(PacketValidationError):
        parse_packet(wrong_type)


@pytest.mark.parametrize(
    "field",
    [
        "user_objective",
        "applicable_constraints",
        "specification",
        "architecture_summary",
        "acceptance_criteria",
        "test_matrix",
        "implementation_slices",
    ],
)
def test_packet_rejects_empty_core_context(field: str) -> None:
    with pytest.raises(PacketValidationError):
        parse_packet(_raw_packet(**{field: ""}))


def test_packet_rejects_unknown_review_kind_without_retaining_it() -> None:
    secret = "SYNTHETIC_SECRET_DO_NOT_EXPOSE_6B2F"

    with pytest.raises(PacketValidationError) as caught:
        parse_packet(_raw_packet(review_kind=secret))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in repr(caught.value.args)


def test_packet_enforces_review_kind_specific_implementation_evidence() -> None:
    with pytest.raises(PacketValidationError):
        parse_packet(_raw_packet(implementation_diff="unexpected"))

    with pytest.raises(PacketValidationError):
        parse_packet(_raw_packet(review_kind="implementation"))

    packet = parse_packet(
        _raw_packet(
            review_kind="implementation",
            implementation_diff="diff --git a/example b/example",
            verification_summary="One test passed.",
        )
    )

    assert packet.review_kind is ReviewKind.IMPLEMENTATION


def test_packet_rejects_canonical_output_larger_than_128_kib() -> None:
    oversized = "x" * MAX_PACKET_BYTES

    with pytest.raises(PacketValidationError):
        parse_packet(_raw_packet(specification=oversized))


def test_packet_accepts_exactly_128_kib_and_rejects_one_more_byte() -> None:
    initial = parse_packet(_raw_packet())
    initial_size = len(canonical_packet_bytes(initial))
    padding = MAX_PACKET_BYTES - initial_size
    exact_text = initial.specification + ("x" * padding)

    exact = parse_packet(_raw_packet(specification=exact_text))
    assert len(canonical_packet_bytes(exact)) == MAX_PACKET_BYTES

    with pytest.raises(PacketValidationError):
        parse_packet(_raw_packet(specification=exact_text + "x"))


def test_packet_rejects_a_lone_surrogate_as_sanitized_validation_error() -> None:
    raw = _raw_packet().replace(
        b'"Review a synthetic design."',
        b'"\\ud800"',
    )

    with pytest.raises(PacketValidationError) as caught:
        parse_packet(raw)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_preview_binds_exact_bytes_and_discloses_external_transmission() -> None:
    packet = parse_packet(_raw_packet())
    canonical = canonical_packet_bytes(packet)

    preview = build_preview(packet)

    assert preview.canonical_packet == canonical.decode("utf-8")
    assert preview.digest == hashlib.sha256(canonical).hexdigest()
    assert preview.byte_size == len(canonical)
    assert preview.model == "claude-opus-5"
    assert preview.effort == "high"
    assert preview.categories == tuple(sorted(_packet()))
    assert "leave this machine" in preview.outbound_data_warning
    notice = preview.retention_notice.lower()
    assert "30 days" in notice
    assert "retained longer" in notice
    assert "model training" in notice


def test_digest_rejects_a_one_byte_change() -> None:
    canonical = canonical_packet_bytes(parse_packet(_raw_packet()))
    approved = hashlib.sha256(canonical).hexdigest()

    require_approved_digest(canonical, approved)

    changed = canonical.replace(b"blue", b"cyan", 1)
    with pytest.raises(PacketValidationError):
        require_approved_digest(changed, approved)


def test_packet_error_traceback_does_not_retain_raw_packet() -> None:
    import json

    secret = "SYNTHETIC_PACKET_TRACEBACK_SECRET_6B2F"
    value = _packet()
    value["extra"] = secret

    with pytest.raises(PacketValidationError) as caught:
        parse_packet(json.dumps(value).encode())

    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_filename.endswith("claude_qc/packet.py"):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
