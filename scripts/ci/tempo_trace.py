"""Structurally verify the bounded Tempo trace contract used by local smoke tests."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _attribute_values(value: object) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for candidate in _sequence(value):
        attribute = _mapping(candidate)
        if attribute is None:
            continue
        key = attribute.get("key")
        wrapped_value = _mapping(attribute.get("value"))
        if not isinstance(key, str) or wrapped_value is None:
            continue
        for value_key in (
            "stringValue",
            "intValue",
            "doubleValue",
            "boolValue",
        ):
            scalar = wrapped_value.get(value_key)
            if isinstance(scalar, (str, int, float, bool)):
                attributes[key] = str(scalar)
                break
    return attributes


def _trace_batches(payload: Mapping[str, object]) -> Sequence[object]:
    batches = _sequence(payload.get("batches"))
    if batches:
        return batches
    return _sequence(payload.get("resourceSpans"))


def _normalized_identifier(value: object, byte_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        decoded = b""
    if len(decoded) == byte_length:
        return decoded.hex()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return decoded.hex() if len(decoded) == byte_length else None


@dataclass(frozen=True)
class TraceObservation:
    """Summarize structural worker, sink, and source-link trace evidence."""

    trace_id: str
    services: tuple[str, ...]
    span_names: tuple[str, ...]
    span_count: int
    worker_span_count: int
    sink_span_count: int
    worker_source_link_count: int
    sink_parented_to_worker_count: int

    @property
    def complete(self) -> bool:
        return (
            self.worker_span_count > 0
            and self.sink_span_count > 0
            and self.worker_source_link_count > 0
            and self.sink_parented_to_worker_count > 0
        )

    @property
    def marker(self) -> str:
        worker = int(self.worker_span_count > 0)
        sink = int(self.sink_span_count > 0)
        source_link = int(self.worker_source_link_count > 0)
        parented = int(self.sink_parented_to_worker_count > 0)
        return f"tempo_trace_w{worker}_s{sink}_l{source_link}_p{parented}"

    def diagnostic(self) -> str:
        return json.dumps(
            {"marker": self.marker, **asdict(self)},
            separators=(",", ":"),
            sort_keys=True,
        )


def inspect_trace(payload: object, trace_id: str) -> TraceObservation:
    """Return safe structural evidence for one exact Tempo trace response."""
    document = _mapping(payload)
    expected_trace_id = _normalized_identifier(trace_id, 16) or trace_id.lower()
    services: set[str] = set()
    span_names: set[str] = set()
    worker_span_ids: set[str] = set()
    sink_parent_ids: list[str] = []
    span_count = 0
    worker_span_count = 0
    sink_span_count = 0
    worker_source_link_count = 0

    for candidate in _trace_batches(document or {}):
        batch = _mapping(candidate)
        if batch is None:
            continue
        resource = _mapping(batch.get("resource")) or {}
        service = _attribute_values(resource.get("attributes")).get("service.name", "")
        if service:
            services.add(service)

        scopes = _sequence(batch.get("scopeSpans")) or _sequence(
            batch.get("instrumentationLibrarySpans")
        )
        for scope_candidate in scopes:
            scope = _mapping(scope_candidate)
            if scope is None:
                continue
            for span_candidate in _sequence(scope.get("spans")):
                span = _mapping(span_candidate)
                if span is None:
                    continue
                span_trace_id = _normalized_identifier(span.get("traceId"), 16)
                if span_trace_id != expected_trace_id:
                    continue

                span_count += 1
                name = span.get("name")
                if isinstance(name, str) and name:
                    span_names.add(name)

                if service == "reasoning-worker":
                    worker_span_count += 1
                    span_id = _normalized_identifier(span.get("spanId"), 8)
                    if span_id is not None:
                        worker_span_ids.add(span_id)
                    for link_candidate in _sequence(span.get("links")):
                        link = _mapping(link_candidate)
                        if link is None:
                            continue
                        link_attributes = _attribute_values(link.get("attributes"))
                        if link_attributes.get("pypi.link.type") == "source_ingestion":
                            worker_source_link_count += 1

                if service == "connect-sink":
                    sink_span_count += 1
                    parent_span_id = _normalized_identifier(span.get("parentSpanId"), 8)
                    if parent_span_id is not None:
                        sink_parent_ids.append(parent_span_id)

    return TraceObservation(
        trace_id=expected_trace_id,
        services=tuple(sorted(services)),
        span_names=tuple(sorted(span_names)),
        span_count=span_count,
        worker_span_count=worker_span_count,
        sink_span_count=sink_span_count,
        worker_source_link_count=worker_source_link_count,
        sink_parented_to_worker_count=sum(
            parent_span_id in worker_span_ids for parent_span_id in sink_parent_ids
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Validate a Tempo trace payload read from standard input."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-id", required=True)
    arguments = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    observation = inspect_trace(payload, arguments.trace_id)
    print(observation.diagnostic())
    return 0 if observation.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
