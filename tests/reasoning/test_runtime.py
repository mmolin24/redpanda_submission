from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Never

import pytest
from helpers import event, evidence

from reasoning_worker.app import _Health, _run_worker_iteration
from reasoning_worker.models import FailureRecord, Finding, ReleaseEvent, TraceContext
from reasoning_worker.provider import (
    DeterministicFakeProvider,
    FakeModelProvider,
    OpenAIResponsesProvider,
)
from reasoning_worker.reasoning import ApplicabilityEngine, MaterialityEngine
from reasoning_worker.runtime import (
    ConfluentAcknowledgedPublisher,
    ConsumerRecord,
    PendingRetryLeaseExpired,
    ProcessExitWatchdog,
    ReasoningWorker,
)
from reasoning_worker.telemetry import (
    WorkerTelemetry,
    current_trace_context,
    extract_trace_context,
    inject_trace_context,
)
from reasoning_worker.terminal import TERMINAL_RECORD_MAX_BYTES, encode_json_bytes
from reasoning_worker.workflow import (
    ProcessingBackpressure,
    ReasoningPipeline,
    StaticEnricher,
)


class Consumer:
    def __init__(self, record: ConsumerRecord):
        self.record = record
        self.committed: list[ConsumerRecord] = []
        self.poll_count = 0

    def poll(self, _timeout: float) -> ConsumerRecord | None:
        self.poll_count += 1
        return self.record

    def commit(self, record: ConsumerRecord) -> None:
        self.committed.append(record)
        if self.record == record:
            self.record = None


class Publisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.published = []

    def publish(self, **message):
        if self.fail:
            raise RuntimeError("broker unavailable")
        assert isinstance(message["value"], bytes)
        self.published.append(
            {
                **message,
                "value": json.loads(message["value"]),
                "value_bytes": message["value"],
            }
        )


class RecordingWatchdog:
    def __init__(self):
        self.arms: list[float] = []
        self.cancel_count = 0

    def arm(self, timeout_seconds: float) -> None:
        self.arms.append(timeout_seconds)

    def cancel(self):
        self.cancel_count += 1


def runtime(fail_publish=False, telemetry=None):
    release_event = event()
    headers: list[tuple[str, bytes | str]] = [
        ("traceparent", b"00-11111111111111111111111111111111-2222222222222222-01")
    ]
    record = ConsumerRecord(
        topic="pypi.releases.v1",
        partition=2,
        offset=9,
        key=release_event.event_key,
        value=json.dumps(release_event.to_dict(), default=str),
        headers=headers,
    )
    consumer = Consumer(record)
    publisher = Publisher(fail_publish)
    provider = DeterministicFakeProvider()
    pipeline = ReasoningPipeline(
        enricher=StaticEnricher(evidence(release_event)),
        materiality=MaterialityEngine(provider),
        applicability=ApplicabilityEngine(provider),
    )
    return (
        ReasoningWorker(consumer, publisher, pipeline, telemetry=telemetry),
        consumer,
        publisher,
    )


class FixedTerminalPipeline:
    def __init__(self, terminal: Finding | FailureRecord):
        self.terminal = terminal

    def process(
        self,
        _event: ReleaseEvent,
        /,
        *,
        processing_attempt_id: str | None = None,
        trace_context: TraceContext | None = None,
    ) -> Finding | FailureRecord:
        return self.terminal


class NeverCalledPipeline:
    def process(
        self,
        _event: ReleaseEvent,
        /,
        *,
        processing_attempt_id: str | None = None,
        trace_context: TraceContext | None = None,
    ) -> Never:
        pytest.fail("invalid release reached enrichment/model processing")


def oversized_runtime(*, fail_publish=False, max_bytes=TERMINAL_RECORD_MAX_BYTES):
    worker, consumer, _ = runtime()
    terminal = worker.pipeline.process(event())
    assert isinstance(terminal, Finding)
    terminal = replace(
        terminal,
        evidence_bundle={"padding": "λ" * TERMINAL_RECORD_MAX_BYTES},
    )
    publisher = Publisher(fail_publish)
    return (
        ReasoningWorker(
            consumer,
            publisher,
            FixedTerminalPipeline(terminal),
            terminal_max_bytes=max_bytes,
        ),
        consumer,
        publisher,
        terminal,
    )


def test_each_terminal_record_gets_an_event_scoped_trace_with_lineage():
    worker, consumer, publisher = runtime()
    assert worker.run_once() is True
    outgoing = publisher.published[0]
    trace = extract_trace_context(outgoing["headers"])
    assert trace.trace_id is not None
    assert trace.trace_id != "11111111111111111111111111111111"
    headers = dict(outgoing["headers"])
    assert headers["source_partition"] == b"2"
    assert headers["source_offset"] == b"9"
    assert len(consumer.committed) == 1
    metrics = worker.telemetry.metrics.render().decode()
    assert 'pypi_reasoning_messages_total{outcome="finding"} 1' in metrics


def test_runtime_emits_trace_correlated_json_logs(caplog):
    worker, _, publisher = runtime()
    with caplog.at_level(logging.INFO, logger="reasoning-worker.runtime"):
        assert worker.run_once() is True

    entries = [json.loads(record.message) for record in caplog.records]
    trace = extract_trace_context(publisher.published[0]["headers"])
    assert [entry["event"] for entry in entries] == [
        "analysis_started",
        "analysis_terminal",
        "terminal_published",
        "source_committed",
    ]
    assert {entry["trace_id"] for entry in entries} == {trace.trace_id}
    assert {entry["event_key"] for entry in entries} == {"pypi:dependency-b:2.0.0"}
    assert entries[-1]["finding_id"] == publisher.published[0]["key"]


def test_events_with_the_same_source_trace_receive_distinct_analysis_traces():
    first_worker, _, first_publisher = runtime()
    second_worker, _, second_publisher = runtime()

    assert first_worker.run_once() is True
    assert second_worker.run_once() is True

    first = extract_trace_context(first_publisher.published[0]["headers"])
    second = extract_trace_context(second_publisher.published[0]["headers"])
    assert first.trace_id != second.trace_id


def test_publish_acknowledgement_precedes_commit():
    worker, consumer, _ = runtime(fail_publish=True)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        worker.run_once()
    assert consumer.committed == []
    assert 'pypi_reasoning_messages_total{outcome="finding"}' not in (
        worker.telemetry.metrics.render().decode()
    )


def test_successful_publish_acknowledgement_strictly_precedes_commit():
    sequence = []
    worker, consumer, _ = runtime()

    class SequencedPublisher(Publisher):
        def publish(self, **message):
            super().publish(**message)
            sequence.append("broker_acknowledged")

    class SequencedConsumer(Consumer):
        def commit(self, record):
            sequence.append("source_committed")
            super().commit(record)

    record = consumer.record
    assert record is not None
    sequenced_consumer = SequencedConsumer(record)
    worker.consumer = sequenced_consumer
    worker.publisher = SequencedPublisher()

    assert worker.run_once() is True

    assert sequence == ["broker_acknowledged", "source_committed"]


def test_oversized_finding_is_acknowledged_as_bounded_failure_before_commit(
    caplog,
):
    worker, consumer, publisher, original = oversized_runtime()

    with caplog.at_level(logging.INFO, logger="reasoning-worker.runtime"):
        assert worker.run_once() is True

    message = publisher.published[0]
    document = message["value"]
    headers = dict(message["headers"])
    assert message["topic"] == "pypi.failures.v1"
    assert len(message["value_bytes"]) <= TERMINAL_RECORD_MAX_BYTES
    assert document["error_class"] == "terminal_record_too_large"
    assert document["payload"]["oversized_terminal"]["original_key"] == (original.finding_id)
    assert headers["source_topic"] == b"pypi.releases.v1"
    assert headers["source_partition"] == b"2"
    assert headers["source_offset"] == b"9"
    assert len(consumer.committed) == 1
    assert consumer.committed[0].offset == 9
    metrics = worker.telemetry.metrics.render().decode()
    assert 'pypi_reasoning_terminal_oversize_total{original_type="finding"} 1' in metrics
    entries = [json.loads(record.message) for record in caplog.records]
    oversize = next(
        entry for entry in entries if entry["event"] == "terminal_oversize_dead_lettered"
    )
    assert oversize["original_type"] == "finding"
    assert oversize["original_bytes"] > TERMINAL_RECORD_MAX_BYTES
    assert "content_sha256" not in oversize


def test_oversize_fallback_encoding_failure_is_systemic_backpressure():
    worker, consumer, publisher, _ = oversized_runtime(max_bytes=128)

    with pytest.raises(ProcessingBackpressure) as error:
        worker.run_once()

    assert error.value.stage == "publication"
    assert error.value.error_class == "terminal_encoding_invalid"
    assert error.value.retryable is False
    assert publisher.published == []
    assert consumer.committed == []
    assert consumer.poll_count == 1
    assert (
        'pypi_reasoning_backpressure_total{kind="systemic"} 1'
        in worker.telemetry.metrics.render().decode()
    )


def test_strict_terminal_encoding_failure_is_unacknowledged_backpressure():
    worker, consumer, publisher = runtime()
    terminal = worker.pipeline.process(event())
    worker.pipeline = FixedTerminalPipeline(
        replace(terminal, evidence_bundle={"ratio": float("nan")})
    )

    with pytest.raises(ProcessingBackpressure) as error:
        worker.run_once()

    assert error.value.error_class == "terminal_encoding_invalid"
    assert publisher.published == []
    assert consumer.committed == []


def test_invalid_utf8_terminal_is_observable_unacknowledged_backpressure(
    caplog,
):
    worker, consumer, publisher = runtime()
    terminal = worker.pipeline.process(event())
    worker.pipeline = FixedTerminalPipeline(
        replace(terminal, evidence_bundle={"invalid_utf8": "\ud800"})
    )

    with caplog.at_level(logging.ERROR, logger="reasoning-worker.runtime"):
        with pytest.raises(ProcessingBackpressure) as error:
            worker.run_once()

    assert error.value.error_class == "terminal_encoding_invalid"
    assert publisher.published == []
    assert consumer.committed == []
    assert (
        'pypi_reasoning_backpressure_total{kind="systemic"} 1'
        in worker.telemetry.metrics.render().decode()
    )
    entries = [json.loads(record.message) for record in caplog.records]
    assert [entry["event"] for entry in entries] == ["terminal_encoding_backpressure"]


def test_oversize_dlq_is_not_committed_or_counted_when_publish_fails():
    worker, consumer, publisher, _ = oversized_runtime(fail_publish=True)

    with pytest.raises(RuntimeError, match="broker unavailable"):
        worker.run_once()

    assert len(publisher.published) == 0
    assert consumer.committed == []
    assert "pypi_reasoning_terminal_oversize_total{" not in (
        worker.telemetry.metrics.render().decode()
    )


def test_confluent_publisher_sends_prepared_bytes_without_reserializing():
    class Producer:
        def __init__(self):
            self.value = None

        def produce(self, _topic, **message):
            self.value = message["value"]
            message["on_delivery"](None, object())

        def flush(self, _timeout):
            return 0

    producer = Producer()
    publisher = ConfluentAcknowledgedPublisher(producer)
    encoded = encode_json_bytes({"multibyte": "λ🚀"})

    publisher.publish(
        topic="pypi.findings.v1",
        key="finding-key",
        value=encoded,
        headers=[],
    )

    assert producer.value is encoded


def test_transient_enrichment_failure_retries_same_offset_after_recovery(caplog):
    release_event = event()
    record = ConsumerRecord(
        topic="pypi.releases.v1",
        partition=2,
        offset=9,
        key=release_event.event_key,
        value=json.dumps(release_event.to_dict(), default=str),
        headers=[],
    )
    consumer = Consumer(record)
    publisher = Publisher()
    watchdog = RecordingWatchdog()

    class FailOnceEnricher:
        def __init__(self):
            self.attempts = 0

        def enrich(self, _event):
            assert len(watchdog.arms) == 1
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("PyPI unavailable")
            return evidence(release_event)

    pipeline = ReasoningPipeline(
        enricher=FailOnceEnricher(),
        materiality=MaterialityEngine(DeterministicFakeProvider()),
        applicability=ApplicabilityEngine(DeterministicFakeProvider()),
    )
    worker = ReasoningWorker(
        consumer,
        publisher,
        pipeline,
        lease_watchdog=watchdog,
    )

    with caplog.at_level(logging.INFO, logger="reasoning-worker.runtime"):
        with pytest.raises(ProcessingBackpressure):
            worker.run_once()

    assert publisher.published == []
    assert consumer.committed == []
    assert consumer.record == record
    assert len(watchdog.arms) == 1
    assert watchdog.cancel_count == 0
    retry_log = json.loads(caplog.records[-1].message)
    assert retry_log["event"] == "analysis_retry_pending"
    assert retry_log["failure_kind"] == "transient"
    assert 'pypi_reasoning_backpressure_total{kind="transient"} 1' in (
        worker.telemetry.metrics.render().decode()
    )

    assert worker.run_once() is True
    assert len(publisher.published) == 1
    assert consumer.committed == [record]
    assert consumer.record is None
    assert consumer.poll_count == 1
    assert len(watchdog.arms) == 1
    assert watchdog.cancel_count == 1


def test_pending_retry_exits_before_the_kafka_ownership_lease_expires():
    release_event = event()
    record = ConsumerRecord(
        "pypi.releases.v1",
        1,
        7,
        release_event.event_key,
        json.dumps(release_event.to_dict(), default=str),
        [],
    )
    consumer = Consumer(record)
    publisher = Publisher()
    now = [0.0]

    class UnavailableEnricher:
        def enrich(self, _event):
            raise TimeoutError("PyPI unavailable")

    pipeline = ReasoningPipeline(
        enricher=UnavailableEnricher(),
        materiality=MaterialityEngine(DeterministicFakeProvider()),
        applicability=ApplicabilityEngine(DeterministicFakeProvider()),
    )
    worker = ReasoningWorker(
        consumer,
        publisher,
        pipeline,
        max_record_age_seconds=10,
        clock=lambda: now[0],
    )

    with pytest.raises(ProcessingBackpressure):
        worker.run_once()
    now[0] = 10
    with pytest.raises(PendingRetryLeaseExpired):
        worker.run_once()

    assert consumer.poll_count == 1
    assert publisher.published == []
    assert consumer.committed == []


def test_record_watchdog_hard_exits_a_process_stuck_past_its_lease():
    command = (
        "import logging\n"
        "import threading\n"
        "import time\n"
        "from reasoning_worker.runtime import ProcessExitWatchdog\n"
        "class BlockedHandler(logging.Handler):\n"
        "    def emit(self, _record):\n"
        "        threading.Event().wait()\n"
        "logging.getLogger('reasoning-worker.runtime').addHandler(BlockedHandler())\n"
        "watchdog = ProcessExitWatchdog()\n"
        "watchdog.arm(0.05)\n"
        "time.sleep(5)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == ProcessExitWatchdog.EXIT_CODE


def test_recovered_work_crossing_the_record_lease_cannot_publish_or_commit():
    release_event = event()
    record = ConsumerRecord(
        "pypi.releases.v1",
        1,
        8,
        release_event.event_key,
        json.dumps(release_event.to_dict(), default=str),
        [],
    )
    consumer = Consumer(record)
    publisher = Publisher()
    now = [0.0]

    class RecoveringEnricher:
        def __init__(self):
            self.attempts = 0

        def enrich(self, _event):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("PyPI unavailable")
            now[0] = 10.1
            return evidence(release_event)

    pipeline = ReasoningPipeline(
        enricher=RecoveringEnricher(),
        materiality=MaterialityEngine(DeterministicFakeProvider()),
        applicability=ApplicabilityEngine(DeterministicFakeProvider()),
    )
    worker = ReasoningWorker(
        consumer,
        publisher,
        pipeline,
        max_record_age_seconds=10,
        clock=lambda: now[0],
    )

    with pytest.raises(ProcessingBackpressure):
        worker.run_once()
    now[0] = 9.9
    with pytest.raises(PendingRetryLeaseExpired):
        worker.run_once()

    assert consumer.poll_count == 1
    assert publisher.published == []
    assert consumer.committed == []


def test_actual_openai_incomplete_response_recovers_through_production_retry_seam():
    release_event = event()
    record = ConsumerRecord(
        "pypi.releases.v1",
        0,
        4,
        release_event.event_key,
        json.dumps(release_event.to_dict(), default=str),
        [],
    )
    consumer = Consumer(record)
    publisher = Publisher()

    class ScriptedResponses:
        def __init__(self):
            self.statuses = ["incomplete", "completed"]

        def create(self, **_body):
            status = self.statuses.pop(0)
            output = (
                {}
                if status == "incomplete"
                else {
                    "decision": "non_substantive",
                    "change_types": [],
                    "claims": [],
                    "missing_evidence": [],
                    "confidence": 0.9,
                }
            )
            return SimpleNamespace(
                id=f"resp_{status}",
                status=status,
                model="gpt-5.6-sol-2026-07-01",
                service_tier="default",
                output=[],
                output_text=json.dumps(output),
                usage=SimpleNamespace(
                    input_tokens=10,
                    input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                    output_tokens=5,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=1),
                ),
                incomplete_details=(
                    SimpleNamespace(reason="max_output_tokens") if status == "incomplete" else None
                ),
                _request_id=f"req_{status}",
            )

    class ScriptedOpenAIClient:
        def __init__(self):
            self.responses = ScriptedResponses()

        def with_options(self, **_options):
            return self

    provider = OpenAIResponsesProvider(
        ScriptedOpenAIClient(),
        sleep=lambda _seconds: None,
    )
    pipeline = ReasoningPipeline(
        enricher=StaticEnricher(evidence(release_event)),
        materiality=MaterialityEngine(provider),
        applicability=ApplicabilityEngine(provider),
    )
    worker = ReasoningWorker(consumer, publisher, pipeline)
    sleeps = []
    _Health.ready = True

    assert (
        _run_worker_iteration(
            worker,
            retry_backoff_seconds=1,
            sleeper=sleeps.append,
        )
        is False
    )
    assert _Health.ready is False
    assert publisher.published == []
    assert consumer.committed == []

    assert (
        _run_worker_iteration(
            worker,
            retry_backoff_seconds=1,
            sleeper=sleeps.append,
        )
        is True
    )
    assert _Health.ready is True
    assert consumer.poll_count == 1
    assert len(publisher.published) == 1
    assert consumer.committed == [record]


def test_invalid_release_payload_becomes_terminal_failure_then_commits():
    record = ConsumerRecord(
        topic="pypi.releases.v1",
        partition=0,
        offset=0,
        key="poison",
        value="{}",
        headers=[("traceparent", b"00-11111111111111111111111111111111-2222222222222222-01")],
    )
    consumer = Consumer(record)
    publisher = Publisher()
    provider = FakeModelProvider([])
    pipeline = ReasoningPipeline(
        enricher=StaticEnricher(evidence()),
        materiality=MaterialityEngine(provider),
        applicability=ApplicabilityEngine(provider),
    )
    worker = ReasoningWorker(consumer, publisher, pipeline)
    assert worker.run_once() is True
    terminal = publisher.published[0]
    assert terminal["topic"] == "pypi.failures.v1"
    assert terminal["value"]["error_class"] == "invalid_release_event"
    assert terminal["value"]["retryable"] is False
    assert terminal["value"]["event_key"] is None
    assert terminal["value"]["payload"]["payload_sha256"].startswith("sha256:")
    expected_fingerprint = terminal["value"]["failure_fingerprint"]
    assert expected_fingerprint.startswith("sha256:")
    assert "{}" not in str(terminal["value"]["payload"])
    assert len(consumer.committed) == 1

    repeated_consumer = Consumer(record)
    repeated_publisher = Publisher()
    repeated_worker = ReasoningWorker(repeated_consumer, repeated_publisher, pipeline)

    assert repeated_worker.run_once() is True
    repeated = repeated_publisher.published[0]["value"]
    assert repeated["failure_id"] != terminal["value"]["failure_id"]
    assert repeated["failure_fingerprint"] == expected_fingerprint


@pytest.mark.parametrize(
    "invalid_value",
    [
        pytest.param(
            lambda document: document.update({"schema_version": "release-event.v2"}),
            id="unsupported-schema-version",
        ),
        pytest.param(
            lambda document: document["package"].pop("normalized_name"),
            id="missing-required-field",
        ),
        pytest.param(
            lambda document: document["observability"]["stage_summary"][0].update(
                {"non_finite": float("nan")}
            ),
            id="non-standard-json-constant",
        ),
    ],
)
def test_invalid_release_short_circuits_the_pipeline_before_commit(
    invalid_value,
):
    release_event = event()
    document = release_event.to_dict()
    invalid_value(document)
    record = ConsumerRecord(
        topic="pypi.releases.v1",
        partition=0,
        offset=1,
        key=release_event.event_key,
        value=json.dumps(document),
        headers=[],
    )
    consumer = Consumer(record)
    publisher = Publisher()

    worker = ReasoningWorker(consumer, publisher, NeverCalledPipeline())

    assert worker.run_once() is True
    assert len(publisher.published) == 1
    assert publisher.published[0]["value"]["error_class"] == "invalid_release_event"
    payload_bytes = (
        record.value if isinstance(record.value, bytes) else record.value.encode("utf-8")
    )
    assert publisher.published[0]["value"]["payload"] == {
        "payload_sha256": ("sha256:" + hashlib.sha256(payload_bytes).hexdigest())
    }
    assert consumer.committed == [record]


def test_release_event_decode_trusts_connect_validated_field_content():
    release_event = event()
    document = release_event.to_dict()
    document["source"] = "another-source"
    document["release"].update(
        {
            "published_at": "source-owned timestamp",
            "url": "source-owned URL",
        }
    )
    document["ingested_at"] = "source-owned ingestion time"
    record = ConsumerRecord(
        topic="pypi.releases.v1",
        partition=0,
        offset=2,
        key=release_event.event_key,
        value=json.dumps(document),
        headers=[],
    )
    consumer = Consumer(record)
    publisher = Publisher()
    worker, _, _ = runtime()
    terminal = worker.pipeline.process(release_event)

    class RecordingPipeline(FixedTerminalPipeline):
        def __init__(self, terminal: Finding | FailureRecord):
            super().__init__(terminal)
            self.received_event: ReleaseEvent | None = None

        def process(
            self,
            event: ReleaseEvent,
            /,
            *,
            processing_attempt_id: str | None = None,
            trace_context: TraceContext | None = None,
        ) -> Finding | FailureRecord:
            self.received_event = event
            return self.terminal

    pipeline = RecordingPipeline(terminal)
    worker = ReasoningWorker(consumer, publisher, pipeline)

    assert worker.run_once() is True

    assert pipeline.received_event is not None
    assert pipeline.received_event.event_key == release_event.event_key
    assert pipeline.received_event.source == document["source"]
    assert pipeline.received_event.release.published_at == "source-owned timestamp"
    assert pipeline.received_event.release.url == "source-owned URL"
    assert pipeline.received_event.ingested_at == "source-owned ingestion time"
    assert publisher.published[0]["topic"] == "pypi.findings.v1"
    assert consumer.committed == [record]


def test_unexpected_release_parser_error_is_systemic_and_uncommitted(
    monkeypatch,
):
    release_event = event()
    record = ConsumerRecord(
        topic="pypi.releases.v1",
        partition=0,
        offset=1,
        key=release_event.event_key,
        value=json.dumps(release_event.to_dict()),
        headers=[],
    )
    consumer = Consumer(record)
    publisher = Publisher()

    def unexpected_parser_failure(_value):
        raise ValueError("validator implementation defect")

    monkeypatch.setattr(ReleaseEvent, "from_dict", unexpected_parser_failure)

    with pytest.raises(ValueError, match="implementation defect"):
        ReasoningWorker(consumer, publisher, NeverCalledPipeline()).run_once()
    assert publisher.published == []
    assert consumer.committed == []


def test_invalid_release_failure_is_not_committed_when_terminal_publish_fails():
    record = ConsumerRecord("pypi.releases.v1", 0, 0, "poison", "not-json", [])
    consumer = Consumer(record)
    publisher = Publisher(fail=True)
    provider = FakeModelProvider([])
    pipeline = ReasoningPipeline(
        enricher=StaticEnricher(evidence()),
        materiality=MaterialityEngine(provider),
        applicability=ApplicabilityEngine(provider),
    )
    with pytest.raises(RuntimeError, match="broker unavailable"):
        ReasoningWorker(consumer, publisher, pipeline).run_once()
    assert consumer.committed == []


def test_invalid_traceparent_is_not_propagated():
    context = extract_trace_context([("traceparent", b"not-valid")])
    assert context.traceparent is None
    assert inject_trace_context([], context) == []


def test_processing_span_starts_new_root_and_links_remote_source_context():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = WorkerTelemetry(provider.get_tracer("test"))
    inbound = extract_trace_context(
        [("traceparent", b"00-11111111111111111111111111111111-2222222222222222-01")]
    )
    with telemetry.processing_span("pypi:dependency-b:2.0.0", inbound):
        outbound = current_trace_context(inbound)
    span = exporter.get_finished_spans()[0]
    assert span.context is not None
    assert f"{span.context.trace_id:032x}" != "11111111111111111111111111111111"
    assert span.parent is None
    assert len(span.links) == 1
    assert f"{span.links[0].context.trace_id:032x}" == "11111111111111111111111111111111"
    assert span.links[0].context.is_remote is True
    assert span.links[0].attributes is not None
    assert dict(span.links[0].attributes) == {"pypi.link.type": "source_ingestion"}
    assert outbound.trace_id == f"{span.context.trace_id:032x}"
    assert outbound.span_id == f"{span.context.span_id:016x}"
    assert outbound.span_id != "2222222222222222"


def test_terminal_trace_uses_active_worker_span_not_embedded_source_observability():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = WorkerTelemetry(provider.get_tracer("test"))
    worker, consumer, publisher = runtime(telemetry=telemetry)
    record = consumer.record
    assert record is not None
    source_payload = json.loads(record.value)
    source_payload["observability"]["analysis_trace_id"] = "a" * 32
    source_payload["observability"]["analysis_span_id"] = "b" * 16
    consumer.record = ConsumerRecord(
        topic=record.topic,
        partition=record.partition,
        offset=record.offset,
        key=record.key,
        value=json.dumps(source_payload),
        headers=record.headers,
    )

    assert worker.run_once() is True
    terminal = publisher.published[0]["value"]
    outgoing = extract_trace_context(publisher.published[0]["headers"])
    span = exporter.get_finished_spans()[0]
    assert span.context is not None

    assert terminal["source_event"]["observability"]["analysis_trace_id"] == "a" * 32
    assert terminal["observability"]["analysis_trace_id"] == f"{span.context.trace_id:032x}"
    assert terminal["observability"]["analysis_trace_id"] != "1" * 32
    assert terminal["observability"]["analysis_span_id"] == f"{span.context.span_id:016x}"
    assert outgoing.trace_id == terminal["observability"]["analysis_trace_id"]
    assert outgoing.span_id == terminal["observability"]["analysis_span_id"]
