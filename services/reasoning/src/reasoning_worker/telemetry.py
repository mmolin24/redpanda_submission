"""Propagate traces and record bounded reasoning-worker telemetry."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from .models import ModelCallRecord, TraceContext

_OUTCOMES = {"finding", "failure", "completed", "refused", "incomplete", "error"}
_PURPOSES = {
    "materiality_assessment",
    "materiality_correction",
    "materiality_review",
    "applicability_assessment",
    "applicability_correction",
    "customer_impact_summary",
    "customer_impact_correction",
    "unknown",
}
_TIERS = {"default", "flex", "unknown"}
_BACKPRESSURE_KINDS = {"transient", "systemic"}
_TERMINAL_TYPES = {"finding", "failure"}


def _bounded(value: str | None, allowed: set[str]) -> str:
    return value if value in allowed else "unknown"


def _labels(values: Iterable[tuple[str, str]]) -> str:
    rendered = ",".join(f'{key}="{value}"' for key, value in values)
    return "{" + rendered + "}" if rendered else ""


class WorkerMetrics:
    """Small, dependency-free Prometheus registry with finite label cardinality."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._messages: dict[str, int] = defaultdict(int)
        self._processing_count: dict[str, int] = defaultdict(int)
        self._processing_sum: dict[str, float] = defaultdict(float)
        self._model_calls: dict[tuple[str, str, str], int] = defaultdict(int)
        self._model_count: dict[tuple[str, str], int] = defaultdict(int)
        self._model_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._tokens: dict[tuple[str, str], int] = defaultdict(int)
        self._backpressure: dict[str, int] = defaultdict(int)
        self._terminal_oversize: dict[str, int] = defaultdict(int)

    def record_terminal(self, outcome: str, duration_seconds: float) -> None:
        outcome = _bounded(outcome, _OUTCOMES)
        with self._lock:
            self._messages[outcome] += 1
            self._processing_count[outcome] += 1
            self._processing_sum[outcome] += max(duration_seconds, 0.0)

    def record_model_call(
        self,
        *,
        purpose: str,
        outcome: str,
        service_tier: str | None,
        duration_seconds: float,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> None:
        purpose = _bounded(purpose, _PURPOSES)
        outcome = _bounded(outcome, _OUTCOMES)
        tier = _bounded(service_tier, _TIERS)
        with self._lock:
            self._model_calls[(purpose, outcome, tier)] += 1
            self._model_count[(purpose, outcome)] += 1
            self._model_sum[(purpose, outcome)] += max(duration_seconds, 0.0)
            for token_type, value in (
                ("input", input_tokens),
                ("cached_input", cached_input_tokens),
                ("cache_write", cache_write_tokens),
                ("output", output_tokens),
                ("reasoning", reasoning_tokens),
            ):
                self._tokens[(purpose, token_type)] += max(int(value), 0)

    def record_backpressure(self, *, retryable: bool) -> None:
        kind = "transient" if retryable else "systemic"
        with self._lock:
            self._backpressure[_bounded(kind, _BACKPRESSURE_KINDS)] += 1

    def record_terminal_oversize(self, *, original_type: str) -> None:
        with self._lock:
            self._terminal_oversize[_bounded(original_type, _TERMINAL_TYPES)] += 1

    def render(self) -> bytes:
        with self._lock:
            lines = [
                "# HELP pypi_reasoning_messages_total Terminal records produced by the worker.",
                "# TYPE pypi_reasoning_messages_total counter",
            ]
            for outcome, value in sorted(self._messages.items()):
                lines.append(
                    f"pypi_reasoning_messages_total{_labels((('outcome', outcome),))} {value}"
                )
            lines.extend(
                [
                    "# HELP pypi_reasoning_processing_duration_seconds Worker processing latency.",
                    "# TYPE pypi_reasoning_processing_duration_seconds summary",
                ]
            )
            for outcome, value in sorted(self._processing_count.items()):
                label = _labels((("outcome", outcome),))
                lines.append(f"pypi_reasoning_processing_duration_seconds_count{label} {value}")
                lines.append(
                    f"pypi_reasoning_processing_duration_seconds_sum{label} "
                    f"{self._processing_sum[outcome]:.9f}"
                )
            lines.extend(
                [
                    "# HELP pypi_reasoning_model_calls_total Logical model calls.",
                    "# TYPE pypi_reasoning_model_calls_total counter",
                ]
            )
            for (purpose, outcome, tier), value in sorted(self._model_calls.items()):
                label = _labels(
                    (("purpose", purpose), ("outcome", outcome), ("service_tier", tier))
                )
                lines.append(f"pypi_reasoning_model_calls_total{label} {value}")
            lines.extend(
                [
                    "# HELP pypi_reasoning_model_duration_seconds Logical model-call latency.",
                    "# TYPE pypi_reasoning_model_duration_seconds summary",
                ]
            )
            for (purpose, outcome), value in sorted(self._model_count.items()):
                label = _labels((("purpose", purpose), ("outcome", outcome)))
                lines.append(f"pypi_reasoning_model_duration_seconds_count{label} {value}")
                lines.append(
                    f"pypi_reasoning_model_duration_seconds_sum{label} "
                    f"{self._model_sum[(purpose, outcome)]:.9f}"
                )
            lines.extend(
                [
                    "# HELP pypi_reasoning_model_tokens_total Model tokens by logical purpose.",
                    "# TYPE pypi_reasoning_model_tokens_total counter",
                ]
            )
            for (purpose, token_type), value in sorted(self._tokens.items()):
                label = _labels((("purpose", purpose), ("type", token_type)))
                lines.append(f"pypi_reasoning_model_tokens_total{label} {value}")
            lines.extend(
                [
                    "# HELP pypi_reasoning_backpressure_total Unacknowledged processing faults.",
                    "# TYPE pypi_reasoning_backpressure_total counter",
                ]
            )
            for kind, value in sorted(self._backpressure.items()):
                label = _labels((("kind", kind),))
                lines.append(f"pypi_reasoning_backpressure_total{label} {value}")
            lines.extend(
                [
                    "# HELP pypi_reasoning_terminal_oversize_total Terminal records replaced by bounded failures.",
                    "# TYPE pypi_reasoning_terminal_oversize_total counter",
                ]
            )
            for original_type, value in sorted(self._terminal_oversize.items()):
                label = _labels((("original_type", original_type),))
                lines.append(f"pypi_reasoning_terminal_oversize_total{label} {value}")
            return ("\n".join(lines) + "\n").encode()


class _SpanScope(AbstractContextManager["_SpanScope"]):
    def __init__(self, manager: Any = None, span: Any = None) -> None:
        self.manager = manager
        self.span = span

    def __enter__(self) -> _SpanScope:
        if self.manager:
            self.span = self.manager.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        if exc and self.span:
            self.span.set_attribute("error.type", _bounded_error_type(exc))
        if self.manager:
            # OTel context managers record raw messages and stacktraces when
            # given the original exception. Preserve only a bounded class label.
            return self.manager.__exit__(
                None if exc else exc_type,
                None if exc else exc,
                None if exc else tb,
            )
        return None

    def set_attribute(self, key: str, value: Any) -> None:
        if self.span is not None and value is not None:
            self.span.set_attribute(key, value)

    def set_result(self, call: ModelCallRecord) -> None:
        self.set_attribute("gen_ai.response.model", call.returned_model or "unknown")
        self.set_attribute(
            "gen_ai.response.id",
            call.attempts[-1].response_id if call.attempts else None,
        )
        self.set_attribute("gen_ai.usage.input_tokens", call.usage.input_tokens)
        self.set_attribute("gen_ai.usage.output_tokens", call.usage.output_tokens)
        self.set_attribute("pypi.gen_ai.cached_input_tokens", call.usage.cached_input_tokens)
        self.set_attribute("pypi.gen_ai.cache_write_tokens", call.usage.cache_write_tokens)
        self.set_attribute("pypi.gen_ai.reasoning_tokens", call.usage.reasoning_tokens)
        self.set_attribute("pypi.gen_ai.outcome", call.outcome)
        self.set_attribute("pypi.gen_ai.estimated_cost_usd", call.estimated_cost_usd)
        if call.attempts:
            last = call.attempts[-1]
            self.set_attribute("pypi.openai.request_id", last.openai_request_id)
            self.set_attribute("pypi.openai.client_request_id", last.client_request_id)
            self.set_attribute("pypi.openai.processing_ms", last.openai_processing_ms)

    def set_error(self, error_type: str) -> None:
        self.set_attribute("error.type", error_type)

    def span_id(self) -> str | None:
        if self.span is None:
            return None
        try:
            context = self.span.get_span_context()
            return f"{context.span_id:016x}" if context.is_valid else None
        except AttributeError:
            return None


@dataclass
class WorkerTelemetry:
    """Small OTel compatibility boundary; safe no-op when SDK/exporter is absent."""

    tracer: Any = None
    metrics: WorkerMetrics = field(default_factory=WorkerMetrics)

    def __post_init__(self) -> None:
        if self.tracer is None:
            try:
                from opentelemetry import trace

                self.tracer = trace.get_tracer("pypi-change-reasoning", "0.1.0")
            except ImportError:
                self.tracer = None

    def record_terminal(self, outcome: str, duration_seconds: float) -> None:
        self.metrics.record_terminal(outcome, duration_seconds)

    def record_model_call(self, call: ModelCallRecord, duration_seconds: float) -> None:
        self.metrics.record_model_call(
            purpose=call.purpose,
            outcome=call.outcome,
            service_tier=call.returned_service_tier or call.requested_service_tier,
            duration_seconds=duration_seconds,
            input_tokens=call.usage.input_tokens,
            cached_input_tokens=call.usage.cached_input_tokens,
            cache_write_tokens=call.usage.cache_write_tokens,
            output_tokens=call.usage.output_tokens,
            reasoning_tokens=call.usage.reasoning_tokens,
        )

    def record_backpressure(self, *, retryable: bool) -> None:
        self.metrics.record_backpressure(retryable=retryable)

    def record_terminal_oversize(self, *, original_type: str) -> None:
        self.metrics.record_terminal_oversize(original_type=original_type)

    def model_call_span(self, request: Any, model_call_id: str) -> _SpanScope:
        scope = self._span(f"{request.purpose} {request.model}", "CLIENT")
        scope.__enter__()
        scope.set_attribute("gen_ai.operation.name", request.purpose)
        scope.set_attribute("gen_ai.provider.name", "openai")
        scope.set_attribute("gen_ai.request.model", request.model)
        scope.set_attribute("gen_ai.request.service_tier", request.service_tier.value)
        scope.set_attribute("gen_ai.request.reasoning_effort", request.reasoning_effort.value)
        scope.set_attribute("pypi.model_call.id", model_call_id)
        # Return a scope already carrying attributes; avoid leaking content attributes.
        return _EnteredScope(scope)

    def http_attempt_span(self, attempt_number: int, attempt_id: str) -> _SpanScope:
        scope = self._span("POST /v1/responses", "CLIENT")
        scope.__enter__()
        scope.set_attribute("http.request.method", "POST")
        scope.set_attribute("server.address", "api.openai.com")
        scope.set_attribute("pypi.attempt.number", attempt_number)
        scope.set_attribute("pypi.attempt.id", attempt_id)
        return _EnteredScope(scope)

    def processing_span(self, event_key: str, source: TraceContext) -> _SpanScope:
        root_context = None
        links = None
        try:
            from opentelemetry import trace
            from opentelemetry.context import Context
            from opentelemetry.trace import Link
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            carrier = {}
            if source.traceparent:
                carrier["traceparent"] = source.traceparent
            if source.tracestate:
                carrier["tracestate"] = source.tracestate
            source_context = TraceContextTextMapPropagator().extract(carrier)
            source_span_context = trace.get_current_span(source_context).get_span_context()
            if source_span_context.is_valid:
                links = [Link(source_span_context, {"pypi.link.type": "source_ingestion"})]
            # A Kafka message is the event boundary. Starting a new root prevents a
            # batched Connect poll from merging unrelated package analyses into one trace.
            root_context = Context()
        except ImportError:
            pass
        scope = self._span(
            "process pypi.releases.v1",
            "CONSUMER",
            context=root_context,
            links=links,
        )
        scope.__enter__()
        scope.set_attribute("messaging.system", "kafka")
        scope.set_attribute("messaging.operation.name", "process")
        scope.set_attribute("pypi.event_key", event_key)
        scope.set_attribute("pypi.source.trace_id", source.trace_id)
        return _EnteredScope(scope)

    def _span(
        self,
        name: str,
        kind_name: str,
        context: Any = None,
        links: Any = None,
    ) -> _SpanScope:
        if not self.tracer:
            return _SpanScope()
        try:
            from opentelemetry.trace import SpanKind

            kind = getattr(SpanKind, kind_name)
            return _SpanScope(
                self.tracer.start_as_current_span(
                    name,
                    kind=kind,
                    context=context,
                    links=links,
                )
            )
        except (ImportError, AttributeError):
            return _SpanScope(self.tracer.start_as_current_span(name))


class _EnteredScope(_SpanScope):
    """Delegates to an already-entered scope without entering it twice."""

    def __init__(self, entered: _SpanScope) -> None:
        self.entered = entered
        super().__init__(None, entered.span)

    def __enter__(self) -> _SpanScope:
        return self.entered

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        return self.entered.__exit__(exc_type, exc, tb)


def extract_trace_context(
    headers: list[tuple[str, bytes | str]] | None,
) -> TraceContext:
    """Extract valid W3C trace headers from a consumed Redpanda record."""
    carrier: dict[str, str] = {}
    for key, value in headers or []:
        if key.lower() in {"traceparent", "tracestate"}:
            carrier[key.lower()] = value.decode() if isinstance(value, bytes) else value
    traceparent = carrier.get("traceparent")
    if traceparent and not _valid_traceparent(traceparent):
        traceparent = None
    return TraceContext(traceparent=traceparent, tracestate=carrier.get("tracestate"))


def inject_trace_context(
    headers: list[tuple[str, bytes | str]], context: TraceContext
) -> list[tuple[str, bytes | str]]:
    """Replace trace headers with the supplied terminal trace context."""
    result = [(k, v) for k, v in headers if k.lower() not in {"traceparent", "tracestate"}]
    if context.traceparent:
        result.append(("traceparent", context.traceparent.encode()))
    if context.tracestate:
        result.append(("tracestate", context.tracestate.encode()))
    return result


def current_trace_context(fallback: TraceContext) -> TraceContext:
    """Return the active OpenTelemetry context or a validated fallback."""
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return TraceContext(
                traceparent=f"00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags):02x}",
                tracestate=str(context.trace_state) if context.trace_state else fallback.tracestate,
            )
    except (ImportError, AttributeError):
        pass
    return fallback


def _valid_traceparent(value: str) -> bool:
    parts = value.split("-")
    if len(parts) != 4 or tuple(map(len, parts)) != (2, 32, 16, 2):
        return False
    try:
        int("".join(parts), 16)
    except ValueError:
        return False
    return parts[1] != "0" * 32 and parts[2] != "0" * 16


def _bounded_error_type(exc: BaseException) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return name[:64] or "exception"
