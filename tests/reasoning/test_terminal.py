from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from helpers import event

from reasoning_worker.models import (
    Disposition,
    FailureRecord,
    Finding,
    utc_now,
)
from reasoning_worker.terminal import (
    TERMINAL_RECORD_MAX_BYTES,
    TerminalEncodingError,
    TerminalFallbackTooLarge,
    encode_json_bytes,
    prepare_terminal,
)


def _finding(*, padding_bytes: int, model_calls: list[dict] | None = None) -> Finding:
    release = event()
    return Finding(
        finding_id="sha256:" + ("a" * 64),
        event_key=release.event_key,
        source_event=release.to_dict(),
        disposition=Disposition.PUBLISHABLE,
        analysis_method="model_assisted",
        package=release.package.normalized_name,
        baseline_version="1.9.0",
        candidate_version=release.release.version,
        gate_results={"materiality": {}, "applicability": {}, "customer_impact": {}},
        evidence_bundle={"padding": "λ" * padding_bytes},
        routing={},
        analysis_metadata={
            "analysis_version": "test",
            "model_calls": model_calls or [],
            "started_at": "2026-07-20T12:35:11Z",
            "completed_at": "2026-07-20T12:35:13Z",
        },
        publishable=True,
        observability={
            "processing_attempt_id": "019b0000-0000-7000-8000-000000000001",
            "analysis_trace_id": "1" * 32,
            "analysis_span_id": "2" * 16,
            "trace_flags": "01",
            "tracestate": None,
            "stage_summary": [
                {
                    "stage": "customer_impact",
                    "outcome": "completed",
                    "started_at": "2026-07-20T12:35:11Z",
                    "completed_at": "2026-07-20T12:35:13Z",
                    "attempt": 1,
                    "detail": "test",
                }
            ],
        },
    )


def _failure(*, padding_bytes: int) -> FailureRecord:
    now = utc_now()
    return FailureRecord(
        failure_id="019b0000-0000-7000-8000-000000000002",
        failure_fingerprint="sha256:" + ("b" * 64),
        event_key="pypi:dependency-b:2.0.0",
        stage="materiality",
        error_class="semantic_validation_exhausted",
        message="bounded test failure",
        retryable=False,
        attempt_count=1,
        first_failed_at=now,
        last_failed_at=now,
        payload={"padding": "x" * padding_bytes},
        observability={
            "processing_attempt_id": "019b0000-0000-7000-8000-000000000001",
            "analysis_trace_id": "1" * 32,
            "analysis_span_id": "2" * 16,
            "trace_flags": "01",
            "tracestate": None,
            "stage_summary": [
                {
                    "stage": "materiality",
                    "outcome": "failed",
                    "started_at": now,
                    "completed_at": now,
                    "attempt": 1,
                    "detail": "test",
                }
            ],
        },
    )


def _model_call() -> dict:
    return {
        "model_call_id": "model-call-1",
        "purpose": "materiality_assessment",
        "requested_model": "gpt-5.6-sol",
        "returned_model": "gpt-5.6-sol",
        "requested_service_tier": "default",
        "returned_service_tier": "default",
        "reasoning_effort": "low",
        "request_payload": {"padding": "r" * 500_000},
        "response_payload": {"padding": "s" * 500_000},
        "request_sha256": "sha256:" + ("1" * 64),
        "response_sha256": "sha256:" + ("2" * 64),
        "usage": {
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 5,
            "reasoning_tokens": 2,
            "future_payload_body": "usage-secret-marker",
        },
        "attempts": [
            {
                "attempt_id": "019b0000-0000-7000-8000-000000000003",
                "client_request_id": "019b0000-0000-7000-8000-000000000003",
                "attempt_number": 1,
                "outcome": "completed",
                "started_at": "2026-07-20T12:35:11Z",
                "completed_at": "2026-07-20T12:35:13Z",
                "latency_ms": 2000,
                "response_id": "resp_1",
                "openai_request_id": "req_1",
                "openai_processing_ms": 1900,
                "future_payload_body": "attempt-secret-marker",
            }
        ],
        "outcome": "completed",
        "estimated_cost_usd": 0.001,
        "span_id": "3" * 16,
        "price_table_version": "test",
        "unrecognized_payload_body": {"padding": "z" * 10_000},
    }


def test_strict_encoder_counts_utf8_bytes_and_rejects_non_json_numbers():
    assert encode_json_bytes({"value": "λ🚀"}) == b'{"value":"\xce\xbb\xf0\x9f\x9a\x80"}'

    with pytest.raises(TerminalEncodingError, match="strict JSON"):
        encode_json_bytes({"value": float("nan")})
    with pytest.raises(TerminalEncodingError, match="strict JSON"):
        encode_json_bytes({1: "numeric keys are not JSON object keys"})
    with pytest.raises(TerminalEncodingError, match="strict JSON"):
        encode_json_bytes({"value": "\ud800"})


def test_strict_encoder_normalizes_domain_tuples_as_json_arrays():
    assert encode_json_bytes({"values": ("a", "b")}) == (b'{"values":["a","b"]}')


def test_exact_terminal_boundary_is_delivered_without_replacement():
    terminal = _finding(padding_bytes=10_000)
    encoded = encode_json_bytes(terminal.to_dict())

    prepared = prepare_terminal(
        terminal,
        intended_topic="pypi.findings.v1",
        max_bytes=len(encoded),
    )

    assert prepared.terminal is terminal
    assert prepared.value == encoded
    assert prepared.was_oversized is False
    assert prepared.key == terminal.finding_id


def test_oversized_finding_becomes_one_bounded_hash_manifest_failure():
    model_call = _model_call()
    terminal = _finding(
        padding_bytes=TERMINAL_RECORD_MAX_BYTES,
        model_calls=[model_call],
    )
    original = encode_json_bytes(terminal.to_dict())
    assert len(original) > TERMINAL_RECORD_MAX_BYTES

    prepared = prepare_terminal(
        terminal,
        intended_topic="pypi.findings.v1",
    )
    document = json.loads(prepared.value)
    manifest = document["payload"]["oversized_terminal"]

    assert prepared.was_oversized is True
    assert isinstance(prepared.terminal, FailureRecord)
    assert len(prepared.value) <= TERMINAL_RECORD_MAX_BYTES
    assert document["schema_version"] == "failure.v1"
    assert document["failure_fingerprint"].startswith("sha256:")
    assert document["stage"] == "publication"
    assert document["error_class"] == "terminal_record_too_large"
    assert document["retryable"] is False
    assert manifest == {
        "original_type": "finding",
        "original_schema_version": "finding.v1",
        "original_key": terminal.finding_id,
        "intended_topic": "pypi.findings.v1",
        "content_sha256": "sha256:" + hashlib.sha256(original).hexdigest(),
        "original_bytes": len(original),
        "application_limit_bytes": TERMINAL_RECORD_MAX_BYTES,
    }
    assert document["payload"]["source_event"] == terminal.source_event
    compact_call = document["payload"]["model_calls"][0]
    assert compact_call["request_payload"] is None
    assert compact_call["response_payload"] is None
    assert compact_call["request_sha256"] == model_call["request_sha256"]
    assert compact_call["response_sha256"] == model_call["response_sha256"]
    assert compact_call["usage"] == {
        key: value for key, value in model_call["usage"].items() if key != "future_payload_body"
    }
    assert compact_call["attempts"] == [
        {
            key: value
            for key, value in model_call["attempts"][0].items()
            if key != "future_payload_body"
        }
    ]
    model_calls_manifest = document["payload"]["model_calls_manifest"]
    assert model_calls_manifest == {
        "content_sha256": ("sha256:" + hashlib.sha256(encode_json_bytes([model_call])).hexdigest()),
        "content_bytes": len(encode_json_bytes([model_call])),
        "item_count": 1,
    }
    assert (
        document["observability"]["analysis_trace_id"]
        == (terminal.observability["analysis_trace_id"])
    )
    assert (
        document["observability"]["analysis_span_id"]
        == (terminal.observability["analysis_span_id"])
    )
    assert (
        document["observability"]["processing_attempt_id"]
        == (terminal.observability["processing_attempt_id"])
    )
    assert document["payload"]["stage_summary_manifest"] == {
        "content_sha256": (
            "sha256:"
            + hashlib.sha256(encode_json_bytes(terminal.observability["stage_summary"])).hexdigest()
        ),
        "content_bytes": len(encode_json_bytes(terminal.observability["stage_summary"])),
        "item_count": 1,
    }
    assert "rrrrrrrr" not in prepared.value.decode()
    assert "ssssssss" not in prepared.value.decode()
    assert "zzzzzzzz" not in prepared.value.decode()
    assert "usage-secret-marker" not in prepared.value.decode()
    assert "attempt-secret-marker" not in prepared.value.decode()
    assert "unrecognized_payload_body" not in compact_call

    repeated = prepare_terminal(
        terminal,
        intended_topic="pypi.findings.v1",
    )
    repeated_document = json.loads(repeated.value)
    assert repeated_document["failure_id"] != document["failure_id"]
    assert repeated_document["failure_fingerprint"] == document["failure_fingerprint"]


def test_oversized_failure_uses_the_same_non_recursive_fallback():
    terminal = _failure(padding_bytes=3 * TERMINAL_RECORD_MAX_BYTES)

    prepared = prepare_terminal(
        terminal,
        intended_topic="pypi.failures.v1",
    )
    document = json.loads(prepared.value)

    assert prepared.was_oversized is True
    assert len(prepared.value) <= TERMINAL_RECORD_MAX_BYTES
    assert document["error_class"] == "terminal_record_too_large"
    assert document["payload"]["oversized_terminal"]["original_type"] == "failure"
    assert document["payload"]["original_failure"] == {
        "failure_id": terminal.failure_id,
        "failure_fingerprint": terminal.failure_fingerprint,
        "stage": terminal.stage,
        "error_class": terminal.error_class,
        "retryable": terminal.retryable,
    }
    assert "xxxxxxxx" not in prepared.value.decode()


def test_impossible_fallback_budget_is_systemic_and_never_recurses():
    terminal = _finding(padding_bytes=10_000)

    with pytest.raises(TerminalFallbackTooLarge):
        prepare_terminal(
            terminal,
            intended_topic="pypi.findings.v1",
            max_bytes=128,
        )


def test_non_finite_terminal_value_is_not_reclassified_as_record_oversize():
    terminal = _finding(padding_bytes=1)
    terminal = replace(terminal, evidence_bundle={"ratio": float("inf")})

    with pytest.raises(TerminalEncodingError, match="strict JSON"):
        prepare_terminal(
            terminal,
            intended_topic="pypi.findings.v1",
        )


def test_malformed_model_call_collection_is_not_silently_discarded():
    terminal = _finding(padding_bytes=TERMINAL_RECORD_MAX_BYTES)
    terminal = replace(
        terminal,
        analysis_metadata={
            **terminal.analysis_metadata,
            "model_calls": [{"model_call_id": "valid"}, "invalid"],
        },
    )

    with pytest.raises(TerminalEncodingError, match="model_calls"):
        prepare_terminal(
            terminal,
            intended_topic="pypi.findings.v1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("processing_attempt_id", None),
        ("analysis_trace_id", "not-a-trace"),
        ("analysis_span_id", "not-a-span"),
        ("trace_flags", "xyz"),
        ("stage_summary", []),
    ],
)
def test_invalid_observability_cannot_create_a_schema_invalid_fallback(
    field: str,
    value: object,
):
    terminal = _finding(padding_bytes=TERMINAL_RECORD_MAX_BYTES)
    terminal = replace(
        terminal,
        observability={**terminal.observability, field: value},
    )

    with pytest.raises(TerminalEncodingError, match="observability"):
        prepare_terminal(
            terminal,
            intended_topic="pypi.findings.v1",
        )


def test_large_source_event_retains_identity_and_full_content_manifest():
    terminal = _finding(padding_bytes=TERMINAL_RECORD_MAX_BYTES)
    source_event = {
        **terminal.source_event,
        "observability": {
            **terminal.source_event["observability"],
            "stage_summary": [
                {
                    **terminal.source_event["observability"]["stage_summary"][0],
                    "large_public_field": "x" * (40 * 1024),
                }
            ],
        },
    }
    terminal = replace(terminal, source_event=source_event)
    source_bytes = encode_json_bytes(source_event)

    prepared = prepare_terminal(
        terminal,
        intended_topic="pypi.findings.v1",
    )
    payload = json.loads(prepared.value)["payload"]

    assert "source_event" not in payload
    assert payload["source_event_identity"] == {
        "schema_version": source_event["schema_version"],
        "event_key": source_event["event_key"],
        "package": source_event["package"]["normalized_name"],
        "release": source_event["release"]["version"],
    }
    assert payload["source_event_manifest"] == {
        "content_sha256": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
        "content_bytes": len(source_bytes),
        "item_count": len(source_event),
    }


def test_normal_terminal_rejects_a_source_event_outside_the_wire_contract():
    terminal = _finding(padding_bytes=1)
    terminal = replace(
        terminal,
        source_event={
            **terminal.source_event,
            "unexpected_root_field": True,
        },
    )

    with pytest.raises(TerminalEncodingError, match="source_event"):
        prepare_terminal(
            terminal,
            intended_topic="pypi.findings.v1",
        )
