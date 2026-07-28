"""Create, validate, bound, and serialize terminal findings and failures."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from .ids import opaque_id
from .models import (
    FailureRecord,
    FailureStage,
    Finding,
    Json,
    ReleaseEvent,
    ReleaseEventContractError,
    utc_now,
)
from .sanitization import bounded_text, sanitize

_FINGERPRINT_DOMAIN = "reasoning-failure.v1"
_MAX_CONDITION_BYTES = 4096
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR_CLASS = re.compile(r"^[a-z0-9_]{1,128}$")

FailureConditionKind = Literal[
    "event",
    "invalid_release_payload",
    "oversized_terminal",
]
TerminalType = Literal["finding", "failure"]


class FailureFingerprintError(ValueError):
    """The stable failure identity cannot satisfy the fingerprint contract."""


@dataclass(frozen=True)
class FailureCondition:
    """Describe the stable condition included in a failure fingerprint."""

    kind: FailureConditionKind
    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        rendered = "\0".join((self.kind, *self.values))
        if len(rendered.encode("utf-8")) > _MAX_CONDITION_BYTES:
            raise FailureFingerprintError("failure fingerprint condition exceeds 4096 UTF-8 bytes")

        if self.kind == "event":
            valid = not self.values
        elif self.kind == "invalid_release_payload":
            valid = len(self.values) == 1 and _SHA256.fullmatch(self.values[0]) is not None
        elif self.kind == "oversized_terminal":
            valid = (
                len(self.values) == 3
                and self.values[0] in {"pypi.findings.v1", "pypi.failures.v1"}
                and self.values[1] in {"finding", "failure"}
                and _SHA256.fullmatch(self.values[2]) is not None
            )
        else:
            valid = False
        if not valid:
            raise FailureFingerprintError("invalid stable failure fingerprint condition")

    @classmethod
    def event(cls) -> FailureCondition:
        return cls("event")

    @classmethod
    def invalid_release_payload(cls, payload_sha256: str) -> FailureCondition:
        return cls("invalid_release_payload", (payload_sha256,))

    @classmethod
    def oversized_terminal(
        cls,
        *,
        intended_topic: str,
        original_type: TerminalType,
        logical_identity: str,
    ) -> FailureCondition:
        return cls(
            "oversized_terminal",
            (intended_topic, original_type, logical_identity),
        )


def reasoning_failure_fingerprint(
    *,
    event_key: str | None,
    stage: FailureStage,
    error_class: str,
    condition: FailureCondition | None = None,
) -> str:
    """Generate a stable grouping identity without collapsing occurrences."""
    if _ERROR_CLASS.fullmatch(error_class) is None:
        raise FailureFingerprintError("invalid failure error class")
    selected_condition = condition or FailureCondition.event()
    segments = (
        _FINGERPRINT_DOMAIN,
        stage,
        error_class,
        event_key or "",
        selected_condition.kind,
        *selected_condition.values,
    )
    if any("\0" in segment for segment in segments):
        raise FailureFingerprintError("failure fingerprint fields cannot contain NUL")
    material = "\0".join(segments).encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def new_reasoning_failure(
    *,
    event_key: str | None,
    stage: FailureStage,
    error_class: str,
    message: str,
    retryable: bool,
    attempt_count: int,
    payload: Json,
    observability: Json,
    condition: FailureCondition | None = None,
) -> FailureRecord:
    """Create one sanitized failure occurrence with a stable fingerprint."""
    now = utc_now()
    return FailureRecord(
        failure_id=opaque_id(),
        failure_fingerprint=reasoning_failure_fingerprint(
            event_key=event_key,
            stage=stage,
            error_class=error_class,
            condition=condition,
        ),
        event_key=event_key,
        stage=stage,
        error_class=error_class,
        message=bounded_text(str(sanitize(message)), max_characters=500),
        retryable=retryable,
        attempt_count=attempt_count,
        first_failed_at=now,
        last_failed_at=now,
        payload=payload,
        observability=observability,
    )


TERMINAL_RECORD_MAX_BYTES = 768 * 1024
_COMPACT_SOURCE_EVENT_MAX_BYTES = 32 * 1024
_COMPACT_MODEL_CALLS_MAX_BYTES = 128 * 1024
_MODEL_CALL_AUDIT_FIELDS = (
    "model_call_id",
    "purpose",
    "requested_model",
    "returned_model",
    "requested_service_tier",
    "returned_service_tier",
    "reasoning_effort",
    "request_sha256",
    "response_sha256",
    "outcome",
    "estimated_cost_usd",
    "span_id",
    "price_table_version",
)
_MODEL_USAGE_AUDIT_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)
_MODEL_ATTEMPT_AUDIT_FIELDS = (
    "attempt_id",
    "client_request_id",
    "attempt_number",
    "outcome",
    "started_at",
    "completed_at",
    "latency_ms",
    "response_id",
    "openai_request_id",
    "openai_processing_ms",
    "error_class",
)
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_TRACE_FLAGS_PATTERN = re.compile(r"^[0-9a-f]{2}$")


class TerminalEncodingError(RuntimeError):
    """A terminal cannot be represented by the strict wire contract."""


class TerminalFallbackTooLarge(TerminalEncodingError):
    """The bounded oversize failure cannot fit its own terminal budget."""


@dataclass(frozen=True)
class PreparedTerminal:
    """Hold one validated terminal record ready for broker publication."""

    terminal: Finding | FailureRecord
    key: str
    value: bytes
    was_oversized: bool


def encode_json_bytes(value: Any) -> bytes:
    """Encode the exact deterministic strict JSON bytes sent to Redpanda."""
    try:
        _validate_json_object_keys(value)
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return rendered.encode("utf-8")
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ) as exc:
        raise TerminalEncodingError(
            "terminal value does not satisfy the strict JSON wire contract"
        ) from exc


def _validate_json_object_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _validate_json_object_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_json_object_keys(child)


def prepare_terminal(
    terminal: Finding | FailureRecord,
    *,
    intended_topic: str,
    max_bytes: int = TERMINAL_RECORD_MAX_BYTES,
) -> PreparedTerminal:
    """Encode one terminal or replace an oversized value with one bounded failure."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    original_key = _terminal_key(terminal)
    _source_event(terminal)
    original_value = encode_json_bytes(terminal.to_dict())
    if len(original_value) <= max_bytes:
        return PreparedTerminal(
            terminal=terminal,
            key=original_key,
            value=original_value,
            was_oversized=False,
        )

    fallback = _oversized_failure(
        terminal,
        intended_topic=intended_topic,
        original_key=original_key,
        original_value=original_value,
        max_bytes=max_bytes,
    )
    fallback_value = encode_json_bytes(fallback.to_dict())
    if len(fallback_value) > max_bytes:
        raise TerminalFallbackTooLarge(
            "bounded terminal-size failure exceeds the application limit"
        )
    return PreparedTerminal(
        terminal=fallback,
        key=fallback.failure_id,
        value=fallback_value,
        was_oversized=True,
    )


def _terminal_key(terminal: Finding | FailureRecord) -> str:
    if isinstance(terminal, Finding):
        return terminal.finding_id
    if isinstance(terminal, FailureRecord):
        return terminal.failure_id
    raise TerminalEncodingError("pipeline returned a non-terminal result")


def _oversized_failure(
    terminal: Finding | FailureRecord,
    *,
    intended_topic: str,
    original_key: str,
    original_value: bytes,
    max_bytes: int,
) -> FailureRecord:
    original_type = "finding" if isinstance(terminal, Finding) else "failure"
    original_observability = _validated_observability(terminal)
    payload: Json = {
        "oversized_terminal": {
            "original_type": original_type,
            "original_schema_version": terminal.schema_version,
            "original_key": original_key,
            "intended_topic": intended_topic,
            "content_sha256": ("sha256:" + hashlib.sha256(original_value).hexdigest()),
            "original_bytes": len(original_value),
            "application_limit_bytes": max_bytes,
        }
    }
    source_event = _source_event(terminal)
    if source_event is not None:
        source_bytes = encode_json_bytes(source_event)
        payload["source_event_manifest"] = _content_manifest(
            source_event,
            source_bytes,
        )
        source_identity = _source_event_identity(source_event)
        if source_identity:
            payload["source_event_identity"] = source_identity
        if len(source_bytes) <= _COMPACT_SOURCE_EVENT_MAX_BYTES:
            payload["source_event"] = source_event

    model_calls = _model_calls(terminal)
    compact_calls = [_compact_model_call(call) for call in model_calls]
    if compact_calls:
        compact_call_bytes = encode_json_bytes(compact_calls)
        payload["model_calls_manifest"] = _content_manifest(
            model_calls,
            encode_json_bytes(model_calls),
        )
        if len(compact_call_bytes) <= _COMPACT_MODEL_CALLS_MAX_BYTES:
            payload["model_calls"] = compact_calls

    stage_summary = original_observability["stage_summary"]
    payload["stage_summary_manifest"] = _content_manifest(
        stage_summary,
        encode_json_bytes(stage_summary),
    )

    if isinstance(terminal, FailureRecord):
        payload["original_failure"] = {
            "failure_id": terminal.failure_id,
            "failure_fingerprint": terminal.failure_fingerprint,
            "stage": terminal.stage,
            "error_class": terminal.error_class,
            "retryable": terminal.retryable,
        }

    now = utc_now()
    stages: list[Json] = [
        {
            "stage": "publication",
            "outcome": "failed",
            "started_at": now,
            "completed_at": now,
            "attempt": 1,
            "detail": "terminal_record_exceeded_application_budget",
        }
    ]
    observability = {
        "processing_attempt_id": original_observability["processing_attempt_id"],
        "analysis_trace_id": original_observability["analysis_trace_id"],
        "analysis_span_id": original_observability["analysis_span_id"],
        "trace_flags": original_observability["trace_flags"],
        "tracestate": original_observability["tracestate"],
        "stage_summary": stages,
    }
    logical_identity = (
        terminal.finding_id if isinstance(terminal, Finding) else terminal.failure_fingerprint
    )
    return new_reasoning_failure(
        event_key=terminal.event_key,
        stage="publication",
        error_class="terminal_record_too_large",
        message="terminal record exceeded the application publication limit",
        retryable=False,
        attempt_count=1,
        payload=payload,
        observability=observability,
        condition=FailureCondition.oversized_terminal(
            intended_topic=intended_topic,
            original_type=original_type,
            logical_identity=logical_identity,
        ),
    )


def _source_event(terminal: Finding | FailureRecord) -> Json | None:
    if isinstance(terminal, Finding):
        value = terminal.source_event
    else:
        if "source_event" not in terminal.payload:
            return None
        value = terminal.payload["source_event"]
    if not isinstance(value, dict):
        raise TerminalEncodingError(
            "terminal source_event does not satisfy the publication contract"
        )
    try:
        ReleaseEvent.from_dict(value)
    except ReleaseEventContractError as exc:
        raise TerminalEncodingError(
            "terminal source_event does not satisfy the publication contract"
        ) from exc
    return value


def _source_event_identity(source_event: Json) -> Json:
    identity: Json = {}
    for source_key, target_key in (
        ("schema_version", "schema_version"),
        ("event_key", "event_key"),
    ):
        value = source_event.get(source_key)
        if isinstance(value, str):
            identity[target_key] = value
    package = source_event.get("package")
    if isinstance(package, dict):
        normalized_name = package.get("normalized_name")
        if isinstance(normalized_name, str):
            identity["package"] = normalized_name
    release = source_event.get("release")
    if isinstance(release, dict):
        version = release.get("version")
        if isinstance(version, str):
            identity["release"] = version
    return identity


def _model_calls(terminal: Finding | FailureRecord) -> list[Json]:
    if isinstance(terminal, Finding):
        value = terminal.analysis_metadata.get("model_calls", [])
    else:
        value = terminal.payload.get("model_calls", [])
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, dict) for item in value):
        raise TerminalEncodingError("terminal model_calls do not satisfy the publication contract")
    return [dict(item) for item in value]


def _compact_model_call(call: Json) -> Json:
    compact: Json = {field: call[field] for field in _MODEL_CALL_AUDIT_FIELDS if field in call}
    compact["request_payload"] = None
    compact["response_payload"] = None

    if "usage" in call:
        usage = call["usage"]
        if not isinstance(usage, dict):
            raise TerminalEncodingError(
                "terminal model-call usage does not satisfy the publication contract"
            )
        compact["usage"] = {
            field: usage[field] for field in _MODEL_USAGE_AUDIT_FIELDS if field in usage
        }

    if "attempts" in call:
        attempts = call["attempts"]
        if not isinstance(attempts, (list, tuple)) or not all(
            isinstance(attempt, dict) for attempt in attempts
        ):
            raise TerminalEncodingError(
                "terminal model-call attempts do not satisfy the publication contract"
            )
        compact["attempts"] = [
            {field: attempt[field] for field in _MODEL_ATTEMPT_AUDIT_FIELDS if field in attempt}
            for attempt in attempts
        ]
    return compact


def _validated_observability(
    terminal: Finding | FailureRecord,
) -> Json:
    value = terminal.observability
    processing_attempt_id = value.get("processing_attempt_id")
    trace_id = value.get("analysis_trace_id")
    span_id = value.get("analysis_span_id")
    trace_flags = value.get("trace_flags")
    tracestate = value.get("tracestate")
    stage_summary = value.get("stage_summary")
    valid = (
        isinstance(processing_attempt_id, str)
        and 1 <= len(processing_attempt_id) <= 64
        and isinstance(trace_id, str)
        and _TRACE_ID_PATTERN.fullmatch(trace_id) is not None
        and isinstance(span_id, str)
        and _SPAN_ID_PATTERN.fullmatch(span_id) is not None
        and isinstance(trace_flags, str)
        and _TRACE_FLAGS_PATTERN.fullmatch(trace_flags) is not None
        and (tracestate is None or (isinstance(tracestate, str) and len(tracestate) <= 512))
        and isinstance(stage_summary, list)
        and bool(stage_summary)
        and all(isinstance(stage, dict) for stage in stage_summary)
    )
    if not valid:
        raise TerminalEncodingError(
            "terminal observability does not satisfy the failure publication contract"
        )
    return {
        "processing_attempt_id": processing_attempt_id,
        "analysis_trace_id": trace_id,
        "analysis_span_id": span_id,
        "trace_flags": trace_flags,
        "tracestate": tracestate,
        "stage_summary": stage_summary,
    }


def _content_manifest(value: Any, encoded: bytes) -> Json:
    count = len(value) if isinstance(value, (dict, list, tuple)) else 1
    return {
        "content_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "content_bytes": len(encoded),
        "item_count": count,
    }
