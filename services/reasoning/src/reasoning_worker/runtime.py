"""Consume releases and publish acknowledged terminal outcomes safely."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from .ids import opaque_id
from .models import (
    FailureRecord,
    Finding,
    Json,
    ReleaseEvent,
    ReleaseEventContractError,
    TraceContext,
    utc_now,
)
from .telemetry import (
    WorkerTelemetry,
    current_trace_context,
    extract_trace_context,
    inject_trace_context,
)
from .terminal import (
    TERMINAL_RECORD_MAX_BYTES,
    FailureCondition,
    TerminalEncodingError,
    new_reasoning_failure,
    prepare_terminal,
)
from .workflow import ProcessingBackpressure

LOG = logging.getLogger("reasoning-worker.runtime")


def _reject_json_constant(_value: str) -> None:
    raise ReleaseEventContractError("release event contains a non-standard JSON constant")


def _parse_release_event(payload: bytes) -> ReleaseEvent:
    try:
        document = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ReleaseEventContractError("release event is not valid strict JSON") from exc
    return ReleaseEvent.from_dict(document)


def _log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    payload = {
        "timestamp": utc_now(),
        "level": logging.getLevelName(level).lower(),
        "event": event,
        **{key: value for key, value in fields.items() if value is not None},
    }
    LOG.log(level, json.dumps(payload, separators=(",", ":"), default=str))


@dataclass(frozen=True)
class ConsumerRecord:
    """Represent one Redpanda record and its acknowledgement coordinates."""

    topic: str
    partition: int
    offset: int
    key: bytes | str | None
    value: bytes | str
    headers: list[tuple[str, bytes | str]]


class Consumer(Protocol):
    """Define polling and synchronous offset commit operations."""

    def poll(self, timeout: float, /) -> ConsumerRecord | None: ...
    def commit(self, record: ConsumerRecord, /) -> None: ...


class AcknowledgedPublisher(Protocol):
    """Publish a terminal record only after broker acknowledgement."""

    def publish(
        self,
        *,
        topic: str,
        key: str,
        value: bytes,
        headers: list[tuple[str, bytes | str]],
    ) -> None:
        """Return only after broker acknowledgement; raise on failure."""
        ...


class TerminalPipeline(Protocol):
    """Resolve a release event into one terminal finding or failure."""

    def process(
        self,
        event: ReleaseEvent,
        /,
        *,
        processing_attempt_id: str | None = None,
        trace_context: TraceContext | None = None,
    ) -> Finding | FailureRecord: ...


class LeaseWatchdog(Protocol):
    """Enforce the retained-record processing lease at process scope."""

    def arm(self, timeout_seconds: float, /) -> None: ...
    def cancel(self) -> None: ...


class PendingRetryLeaseExpired(RuntimeError):
    """The process must restart before Kafka can revoke record ownership."""


class ProcessExitWatchdog:
    """Hard-stop a process whose retained record exceeds its ownership lease."""

    EXIT_CODE = 75

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def arm(self, timeout_seconds: float) -> None:
        with self._lock:
            if self._timer is not None:
                return
            timer = threading.Timer(timeout_seconds, self._expire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def cancel(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    def _expire(self) -> None:
        with self._lock:
            if self._timer is None:
                return
            self._timer = None
        # Socket read timeouts are inactivity bounds, so only a process
        # boundary can enforce this ownership lease as wall-clock time. Do not
        # log or perform any other potentially blocking I/O before this exit.
        os._exit(self.EXIT_CODE)


class ReasoningWorker:
    """Delivery invariant: terminal broker acknowledgement strictly precedes commit."""

    def __init__(
        self,
        consumer: Consumer,
        publisher: AcknowledgedPublisher,
        pipeline: TerminalPipeline,
        *,
        telemetry: WorkerTelemetry | None = None,
        findings_topic: str = "pypi.findings.v1",
        failures_topic: str = "pypi.failures.v1",
        max_record_age_seconds: float = 1_800,
        terminal_max_bytes: int = TERMINAL_RECORD_MAX_BYTES,
        clock=monotonic,
        lease_watchdog: LeaseWatchdog | None = None,
    ) -> None:
        if max_record_age_seconds <= 0:
            raise ValueError("max_record_age_seconds must be positive")
        if terminal_max_bytes <= 0:
            raise ValueError("terminal_max_bytes must be positive")
        self.consumer = consumer
        self.publisher = publisher
        self.pipeline = pipeline
        self.telemetry = telemetry or WorkerTelemetry()
        self.findings_topic = findings_topic
        self.failures_topic = failures_topic
        self.max_record_age_seconds = max_record_age_seconds
        self.terminal_max_bytes = terminal_max_bytes
        self.clock = clock
        self.lease_watchdog = lease_watchdog or ProcessExitWatchdog()
        self._pending_record: ConsumerRecord | None = None
        self._record_started_at: float | None = None

    def run_once(self, timeout: float = 1.0) -> bool:
        record = self._pending_record or self.consumer.poll(timeout)
        if record is None:
            return False
        if self._record_started_at is None:
            self._record_started_at = self.clock()
            self.lease_watchdog.arm(self.max_record_age_seconds)
        source_context = extract_trace_context(record.headers)
        # Every release event owns an analysis trace. The source trace remains linked
        # by telemetry, but is not used as the parent because Connect may have split
        # many RSS entries from one poll under the same trace.
        event_fallback_context = TraceContext().ensured()
        processing_attempt_id = opaque_id()
        payload_bytes = (
            record.value if isinstance(record.value, bytes) else record.value.encode("utf-8")
        )
        try:
            event = _parse_release_event(payload_bytes)
        except ReleaseEventContractError:
            event = None
        span_key = event.event_key if event is not None else "invalid-release-event"
        with self.telemetry.processing_span(span_key, source_context):
            processing_started = monotonic()
            outbound_context = current_trace_context(event_fallback_context).ensured()
            log_context = {
                "trace_id": outbound_context.trace_id,
                "span_id": outbound_context.span_id,
                "event_key": span_key,
                "processing_attempt_id": processing_attempt_id,
            }
            _log_event(
                "analysis_started",
                **log_context,
                stage="worker_receive",
                source_topic=record.topic,
                source_partition=record.partition,
                source_offset=record.offset,
            )
            self._ensure_record_lease(record, log_context)
            # Contract failures and valid events converge on the same terminal publish path.
            if event is None:
                terminal = self._invalid_release_failure(
                    payload_bytes, processing_attempt_id, outbound_context
                )
            else:
                try:
                    terminal = self.pipeline.process(
                        event,
                        processing_attempt_id=processing_attempt_id,
                        trace_context=outbound_context,
                    )
                except ProcessingBackpressure as exc:
                    # Backpressure retains the current record and leaves its offset uncommitted.
                    self._pending_record = record
                    failure_kind = "transient" if exc.retryable else "systemic"
                    self.telemetry.record_backpressure(retryable=exc.retryable)
                    _log_event(
                        "analysis_retry_pending",
                        level=logging.ERROR,
                        **log_context,
                        stage=exc.stage,
                        error_class=exc.error_class,
                        failure_kind=failure_kind,
                        source_topic=record.topic,
                        source_partition=record.partition,
                        source_offset=record.offset,
                    )
                    raise
            self._ensure_record_lease(record, log_context)
            # Analysis has converged; terminal type now selects the destination topic.
            if isinstance(terminal, Finding):
                topic, key = self.findings_topic, terminal.finding_id
                terminal_fields = {
                    "terminal_type": "finding",
                    "finding_id": terminal.finding_id,
                    "disposition": terminal.disposition.value,
                    "publishable": terminal.publishable,
                    "model_call_count": len(terminal.analysis_metadata.get("model_calls", [])),
                }
            elif isinstance(terminal, FailureRecord):
                topic, key = self.failures_topic, terminal.failure_id
                terminal_fields = {
                    "terminal_type": "failure",
                    "failure_id": terminal.failure_id,
                    "error_class": terminal.error_class,
                    "retryable": terminal.retryable,
                }
            else:  # pragma: no cover - type-system backstop
                raise TypeError("pipeline returned a non-terminal result")
            original_type = terminal_fields["terminal_type"]
            oversized_failure: FailureRecord | None = None
            try:
                prepared = prepare_terminal(
                    terminal,
                    intended_topic=topic,
                    max_bytes=self.terminal_max_bytes,
                )
                if prepared.was_oversized:
                    if not isinstance(prepared.terminal, FailureRecord):
                        raise TerminalEncodingError(
                            "oversized terminal did not produce a failure record"
                        )
                    oversized_failure = prepared.terminal
            except TerminalEncodingError as exc:
                self._pending_record = record
                self.telemetry.record_backpressure(retryable=False)
                _log_event(
                    "terminal_encoding_backpressure",
                    level=logging.ERROR,
                    **log_context,
                    stage="publication",
                    error_class="terminal_encoding_invalid",
                    failure_kind="systemic",
                    source_topic=record.topic,
                    source_partition=record.partition,
                    source_offset=record.offset,
                )
                raise ProcessingBackpressure(
                    "publication",
                    "terminal_encoding_invalid",
                    "terminal record could not satisfy the strict publication contract",
                    retryable=False,
                ) from exc
            if oversized_failure is not None:
                terminal = oversized_failure
                topic = self.failures_topic
                key = prepared.key
                terminal_fields = {
                    "terminal_type": "failure",
                    "failure_id": oversized_failure.failure_id,
                    "error_class": oversized_failure.error_class,
                    "retryable": oversized_failure.retryable,
                }
            else:
                key = prepared.key
            _log_event(
                "analysis_terminal",
                **log_context,
                **terminal_fields,
                stage="pipeline_terminal",
                duration_ms=round((monotonic() - processing_started) * 1000, 3),
            )
            headers = inject_trace_context([], outbound_context)
            headers.extend(
                [
                    ("source_topic", record.topic.encode()),
                    ("source_partition", str(record.partition).encode()),
                    ("source_offset", str(record.offset).encode()),
                    ("processing_attempt_id", processing_attempt_id.encode()),
                ]
            )
            try:
                self.publisher.publish(
                    topic=topic,
                    key=key,
                    value=prepared.value,
                    headers=headers,
                )
            except Exception as exc:
                _log_event(
                    "terminal_publish_failed",
                    level=logging.ERROR,
                    **log_context,
                    **terminal_fields,
                    stage="terminal_publish",
                    destination_topic=topic,
                    publish_error_class=type(exc).__name__,
                )
                raise
            if oversized_failure is not None:
                oversized_manifest = oversized_failure.payload["oversized_terminal"]
                self.telemetry.record_terminal_oversize(original_type=str(original_type))
                _log_event(
                    "terminal_oversize_dead_lettered",
                    **log_context,
                    stage="terminal_publish",
                    original_type=original_type,
                    original_bytes=oversized_manifest["original_bytes"],
                    application_limit_bytes=self.terminal_max_bytes,
                    destination_topic=topic,
                )
            self.telemetry.record_terminal(
                "finding" if isinstance(terminal, Finding) else "failure",
                monotonic() - processing_started,
            )
            _log_event(
                "terminal_published",
                **log_context,
                **terminal_fields,
                stage="terminal_publish",
                destination_topic=topic,
            )
            # Do not move this before publish: it is the at-least-once safety boundary.
            self.consumer.commit(record)
            self.lease_watchdog.cancel()
            self._pending_record = None
            self._record_started_at = None
            _log_event(
                "source_committed",
                **log_context,
                **terminal_fields,
                stage="source_commit",
                source_topic=record.topic,
                source_partition=record.partition,
                source_offset=record.offset,
            )
        return True

    def _ensure_record_lease(self, record: ConsumerRecord, log_context: Json) -> None:
        if self._record_started_at is None:
            return
        elapsed = self.clock() - self._record_started_at
        if elapsed < self.max_record_age_seconds:
            return
        _log_event(
            "analysis_retry_lease_expired",
            level=logging.ERROR,
            **log_context,
            stage="worker_receive",
            source_topic=record.topic,
            source_partition=record.partition,
            source_offset=record.offset,
            max_record_age_seconds=self.max_record_age_seconds,
        )
        self.lease_watchdog.cancel()
        raise PendingRetryLeaseExpired(
            "pending record exceeded the ownership-safe processing window"
        )

    def close(self) -> None:
        self.lease_watchdog.cancel()

    @staticmethod
    def _invalid_release_failure(
        payload: bytes,
        processing_attempt_id: str,
        trace_context: TraceContext,
    ) -> FailureRecord:
        now = utc_now()
        payload_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
        stages: list[Json] = [
            {
                "stage": "ingestion",
                "outcome": "failed",
                "started_at": now,
                "completed_at": now,
                "attempt": 1,
                "detail": "release_topic_contract_invalid",
            }
        ]
        return new_reasoning_failure(
            event_key=None,
            stage="ingestion",
            error_class="invalid_release_event",
            message="release-topic payload failed worker contract validation",
            retryable=False,
            attempt_count=1,
            payload={"payload_sha256": payload_sha256},
            observability=trace_context.to_observability(processing_attempt_id, stages),
            condition=FailureCondition.invalid_release_payload(payload_sha256),
        )


class KafkaProducer(Protocol):
    """Describe the Kafka-compatible producer methods used with Redpanda."""

    def produce(
        self,
        topic: str,
        /,
        *,
        key: bytes,
        value: bytes,
        headers: (dict[str, str | bytes | None] | list[tuple[str, str | bytes | None]] | None),
        on_delivery: Callable[[object | None, object], None],
    ) -> None: ...

    def flush(self, timeout: float, /) -> int: ...


class ConfluentAcknowledgedPublisher:
    """Thin optional adapter for confluent-kafka with per-record delivery acknowledgement."""

    def __init__(self, producer: KafkaProducer, timeout_seconds: float = 30.0) -> None:
        self.producer = producer
        self.timeout_seconds = timeout_seconds

    def publish(
        self,
        *,
        topic: str,
        key: str,
        value: bytes,
        headers: list[tuple[str, bytes | str]],
    ) -> None:
        error: list[BaseException] = []

        def delivered(err: object | None, _message: object) -> None:
            if err is not None:
                error.append(RuntimeError(str(err)))

        encoded_headers: list[tuple[str, str | bytes | None]] = [
            (k, v.encode() if isinstance(v, str) else v) for k, v in headers
        ]
        self.producer.produce(
            topic,
            key=key.encode(),
            value=value,
            headers=encoded_headers,
            on_delivery=delivered,
        )
        remaining = int(self.producer.flush(self.timeout_seconds))
        if remaining:
            raise TimeoutError(f"{remaining} terminal message(s) were not acknowledged")
        if error:
            raise error[0]
