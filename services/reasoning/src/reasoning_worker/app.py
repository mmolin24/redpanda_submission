"""Assemble and run the Redpanda-backed reasoning worker application."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from .evidence import (
    BoundedHttpByteFetcher,
    BoundedHttpJsonFetcher,
    MetadataEvidenceBuilder,
    PyPIEnricher,
)
from .ids import sha256_json
from .models import ReleaseEvent
from .monitoring import MonitoredPackages, load_monitored_packages
from .provider import (
    DeterministicFakeProvider,
    ModelPayloadCapturePolicy,
    OpenAIResponsesProvider,
)
from .reasoning import (
    ApplicabilityEngine,
    CustomerImpactEngine,
    MaterialityEngine,
)
from .runtime import ConfluentAcknowledgedPublisher, ConsumerRecord, ReasoningWorker
from .telemetry import WorkerTelemetry
from .workflow import Enricher, ProcessingBackpressure, ReasoningPipeline

LOG = logging.getLogger("reasoning-worker")

_CONSUMER_OWNERSHIP_MARGIN_SECONDS = 60


@dataclass
class _IdleDrainCompletion:
    """Recognize a finite fixture batch without weakening retry behavior."""

    required_idle_polls: int
    committed_records: int = 0
    consecutive_idle_polls: int = 0

    def observe(self, *, processed: bool, ready: bool) -> bool:
        """Return true after work was committed and the assigned log stays idle."""
        if processed:
            self.committed_records += 1
            self.consecutive_idle_polls = 0
        elif ready and self.committed_records > 0:
            self.consecutive_idle_polls += 1
        else:
            # Backpressure is not an idle queue and must never complete a batch.
            self.consecutive_idle_polls = 0
        return self.consecutive_idle_polls >= self.required_idle_polls


class FixtureEnricher:
    """Offline deterministic enrichment for the checked-in package history."""

    def __init__(self, history_path: Path) -> None:
        document = json.loads(history_path.read_text())
        events = document.get("events")
        if not isinstance(events, list) or not events:
            raise ValueError("fixture history must contain a non-empty events list")
        self.fixture_version = str(document.get("fixture_version", "unknown"))
        self.events: dict[str, dict[str, Any]] = {}
        for entry in events:
            if not isinstance(entry, dict):
                raise ValueError("fixture history entries must be objects")
            package = str(entry.get("package", ""))
            version = str(entry.get("version", ""))
            event_key = f"pypi:{package}:{version}"
            if not package or not version or event_key in self.events:
                raise ValueError("fixture history contains an invalid or duplicate event")
            self.events[event_key] = entry

    def enrich(self, event: ReleaseEvent):
        entry = self.events.get(event.event_key)
        if entry is None:
            raise ValueError(f"no deterministic fixture evidence for {event.event_key}")
        if (
            entry["package"] != event.package.normalized_name
            or entry["version"] != event.release.version
        ):
            raise ValueError("fixture evidence does not match the release event")

        baseline_spec = dict(entry["baseline"])
        candidate_spec = dict(entry["candidate"])
        baseline_version = str(baseline_spec.pop("version"))
        baseline_urls = self._urls(
            baseline_spec.pop("urls", None),
            package=event.package.normalized_name,
            version=baseline_version,
        )
        candidate_urls = self._urls(
            candidate_spec.pop("urls", None),
            package=event.package.normalized_name,
            version=event.release.version,
        )
        baseline = {
            "info": {
                "name": event.package.normalized_name,
                "version": baseline_version,
                **baseline_spec,
            },
            "urls": baseline_urls,
            "vulnerabilities": entry.get("baseline_vulnerabilities", []),
        }
        candidate = {
            "info": {
                "name": event.package.normalized_name,
                "version": event.release.version,
                **candidate_spec,
            },
            "urls": candidate_urls,
            "vulnerabilities": entry.get("candidate_vulnerabilities", []),
        }
        fixture_context: dict[str, Any] = {
            "repository_mapping_confidence": "unavailable",
            "fixture": {
                "synthetic": True,
                "fixture_version": self.fixture_version,
                "published_at": entry.get("published_at"),
                "scenario_id": entry.get("scenario_id"),
            },
        }
        if entry.get("include_summary_context", True):
            fixture_context["documents"] = [
                {
                    "kind": "fixture_changelog",
                    "text": str(entry["summary"]),
                }
            ]
        return MetadataEvidenceBuilder().build(
            event,
            baseline,
            candidate,
            context=fixture_context,
            provenance=[
                {
                    "source_url": f"fixture://{self.fixture_version}/{event.event_key}",
                    "retrieved_at": event.ingested_at,
                    "content_sha256": sha256_json(entry),
                }
            ],
        )

    @staticmethod
    def _urls(
        value: object,
        *,
        package: str,
        version: str,
    ) -> list[dict[str, Any]]:
        if value is None:
            return [
                {
                    "filename": f"{package}-{version}-py3-none-any.whl",
                    "packagetype": "bdist_wheel",
                    "python_version": "py3",
                }
            ]
        if not isinstance(value, list) or not value:
            raise ValueError("fixture urls must be a non-empty list")
        if not all(isinstance(item, dict) and item.get("filename") for item in value):
            raise ValueError("fixture urls must contain file objects with filenames")
        return [dict(item) for item in value]


class ConfluentConsumerAdapter:
    """Adapt a Confluent consumer to the worker's delivery-safe interface."""

    def __init__(self, consumer: Any) -> None:
        self.consumer = consumer

    def poll(self, timeout: float) -> ConsumerRecord | None:
        message = self.consumer.poll(timeout)
        if message is None:
            return None
        if message.error():
            raise RuntimeError(str(message.error()))
        return ConsumerRecord(
            topic=message.topic(),
            partition=message.partition(),
            offset=message.offset(),
            key=message.key(),
            value=message.value(),
            headers=message.headers() or [],
        )

    def commit(self, record: ConsumerRecord) -> None:
        from confluent_kafka import TopicPartition

        self.consumer.commit(
            offsets=[TopicPartition(record.topic, record.partition, record.offset + 1)],
            asynchronous=False,
        )

    def close(self) -> None:
        self.consumer.close()


def configure_telemetry() -> WorkerTelemetry:
    """Create worker telemetry with an optional OTLP trace exporter."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return WorkerTelemetry()
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": "reasoning-worker",
                    "deployment.environment": os.getenv("DEPLOYMENT_ENV", "local"),
                }
            )
        )
        traces_url = (
            endpoint if endpoint.endswith("/v1/traces") else endpoint.rstrip("/") + "/v1/traces"
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_url)))
        trace.set_tracer_provider(provider)
        return WorkerTelemetry(trace.get_tracer("pypi-change-reasoning", "0.1.0"))
    except Exception as exc:
        LOG.warning("telemetry exporter unavailable", extra={"error_class": type(exc).__name__})
        return WorkerTelemetry()


def build_pipeline(
    monitored_packages: MonitoredPackages,
    telemetry: WorkerTelemetry,
) -> ReasoningPipeline:
    """Build the configured evidence, model, and reasoning pipeline."""
    model_mode = _model_mode()
    default_evidence_mode = "fixture" if model_mode == "fake" else "pypi"
    evidence_mode = os.getenv("EVIDENCE_MODE", "").lower() or default_evidence_mode
    capture_policy = ModelPayloadCapturePolicy.from_environment()

    if evidence_mode == "fixture":
        history_path = Path(
            os.getenv("FIXTURE_HISTORY_PATH", "/data/fixtures/package-history.json")
        )
        enricher: Enricher = FixtureEnricher(history_path)
    elif evidence_mode == "pypi":
        enricher = PyPIEnricher(
            BoundedHttpJsonFetcher(),
            byte_fetcher=BoundedHttpByteFetcher(),
        )
    else:
        raise ValueError("EVIDENCE_MODE must be fixture or pypi")

    if model_mode == "fake":
        provider = DeterministicFakeProvider(
            telemetry,
            capture_policy=capture_policy,
            delay_seconds=_fake_model_delay_seconds(),
        )
    elif model_mode == "openai":
        from openai import OpenAI

        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required when MODEL_MODE=openai")
        prompt_cache_enabled = os.getenv("OPENAI_PROMPT_CACHE_ENABLED", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        timeout_seconds = _openai_timeout_seconds()
        provider = OpenAIResponsesProvider(
            OpenAI(timeout=timeout_seconds, max_retries=0),
            telemetry=telemetry,
            prompt_cache_enabled=prompt_cache_enabled,
            capture_policy=capture_policy,
        )
    else:
        raise ValueError("MODEL_MODE must resolve to fake or openai")
    return ReasoningPipeline(
        enricher=enricher,
        materiality=MaterialityEngine(provider),
        applicability=ApplicabilityEngine(provider),
        customer_impact=CustomerImpactEngine(provider),
        monitored_packages=monitored_packages,
        analysis_policy_revision=os.getenv("ANALYSIS_POLICY_REVISION", "analysis-policy-v1"),
    )


def _model_mode() -> str:
    """Resolve automatic model selection while retaining an explicit fake override."""
    configured = os.getenv("MODEL_MODE", "auto").lower()
    if configured == "auto":
        return "openai" if os.getenv("OPENAI_API_KEY") else "fake"
    if configured not in {"fake", "openai"}:
        raise ValueError("MODEL_MODE must be auto, fake, or openai")
    return configured


def _fake_model_delay_seconds() -> float:
    raw = os.getenv("FAKE_MODEL_DELAY_SECONDS", "0")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("FAKE_MODEL_DELAY_SECONDS must be a number") from exc
    if not math.isfinite(value) or not 0 <= value <= 30:
        raise ValueError("FAKE_MODEL_DELAY_SECONDS must be between 0 and 30")
    if value and os.getenv("DEPLOYMENT_ENV", "local") != "local":
        raise ValueError("FAKE_MODEL_DELAY_SECONDS is local-only")
    return value


class _Health(BaseHTTPRequestHandler):
    ready = False

    def do_GET(self) -> None:
        status = 200 if self.path == "/live" or (self.path == "/ready" and self.ready) else 503
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok" if status == 200 else "not_ready"}).encode())

    # BaseHTTPRequestHandler requires the exact `format` parameter name.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _health_server() -> ThreadingHTTPServer:
    # This server is container-internal; Compose publishes only loopback ports.
    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.getenv("WORKER_HEALTH_PORT", "8090"))),  # noqa: S104
        _Health,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Metrics(BaseHTTPRequestHandler):
    telemetry: WorkerTelemetry | None = None

    def do_GET(self) -> None:
        if self.path != "/metrics" or self.telemetry is None:
            self.send_response(404)
            self.end_headers()
            return
        body = self.telemetry.metrics.render()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # BaseHTTPRequestHandler requires the exact `format` parameter name.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _metrics_server(telemetry: WorkerTelemetry) -> ThreadingHTTPServer:
    _Metrics.telemetry = telemetry
    # This server is container-internal; Compose publishes only loopback ports.
    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.getenv("WORKER_METRICS_PORT", "8001"))),  # noqa: S104
        _Metrics,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class WorkerIteration(Protocol):
    """Define the bounded worker operation used by the process loop."""

    def run_once(self, timeout: float, /) -> bool: ...


def _run_worker_iteration(
    worker: WorkerIteration,
    *,
    retry_backoff_seconds: float,
    sleeper=time.sleep,
) -> bool:
    try:
        processed = worker.run_once(1.0)
    except ProcessingBackpressure:
        _Health.ready = False
        sleeper(retry_backoff_seconds)
        return False
    _Health.ready = True
    return processed


def _retry_lease_settings() -> tuple[int, float]:
    max_poll_interval_ms = int(os.getenv("CONSUMER_MAX_POLL_INTERVAL_MS", "3600000"))
    max_record_age_seconds = float(os.getenv("PROCESSING_RETRY_MAX_ELAPSED_SECONDS", "1800"))
    ownership_margin_ms = _CONSUMER_OWNERSHIP_MARGIN_SECONDS * 1_000
    if max_poll_interval_ms <= ownership_margin_ms:
        raise ValueError("CONSUMER_MAX_POLL_INTERVAL_MS must exceed 60000")
    if not math.isfinite(max_record_age_seconds):
        raise ValueError("PROCESSING_RETRY_MAX_ELAPSED_SECONDS must be finite")
    if max_record_age_seconds <= 0:
        raise ValueError("PROCESSING_RETRY_MAX_ELAPSED_SECONDS must be positive")
    if max_record_age_seconds * 1_000 >= max_poll_interval_ms - ownership_margin_ms:
        raise ValueError(
            "processing retry window must end at least 60 seconds before max.poll.interval.ms"
        )
    return max_poll_interval_ms, max_record_age_seconds


def _openai_timeout_seconds() -> float:
    timeout_seconds = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
    if not 0 < timeout_seconds <= 60:
        raise ValueError("OPENAI_TIMEOUT_SECONDS must be greater than 0 and at most 60")
    return timeout_seconds


def _retry_backoff_seconds() -> float:
    backoff_seconds = float(os.getenv("PROCESSING_RETRY_BACKOFF_SECONDS", "5"))
    if not math.isfinite(backoff_seconds) or not 0.1 <= backoff_seconds <= 60:
        raise ValueError("PROCESSING_RETRY_BACKOFF_SECONDS must be between 0.1 and 60 seconds")
    return backoff_seconds


def _exit_after_idle_polls() -> int:
    """Read the opt-in finite-batch boundary used by the Compose demo."""
    raw_value = os.getenv("EXIT_AFTER_IDLE_POLLS", "0")
    try:
        idle_polls = int(raw_value)
    except ValueError:
        idle_polls = -1
    if not 0 <= idle_polls <= 60:
        raise ValueError("EXIT_AFTER_IDLE_POLLS must be an integer between 0 and 60")
    return idle_polls


def _producer_config(brokers: str) -> dict[str, object]:
    """Keep broker headroom explicit at the only worker producer boundary."""
    return {
        "bootstrap.servers": brokers,
        "enable.idempotence": True,
        "acks": "all",
        "max.in.flight.requests.per.connection": 1,
        "message.max.bytes": 1_000_000,
    }


def main() -> None:
    """Run the reasoning worker until an interrupt requests graceful shutdown."""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
    from confluent_kafka import Consumer as KafkaConsumer
    from confluent_kafka import Producer

    monitored_packages = load_monitored_packages(
        Path(os.getenv("MONITORED_PACKAGES_PATH", "/config/monitored-packages.json"))
    )
    telemetry = configure_telemetry()
    pipeline = build_pipeline(monitored_packages, telemetry)
    brokers = os.getenv("REDPANDA_BROKERS", "redpanda:9092")
    input_topic = os.getenv("INPUT_TOPIC", "pypi.releases.v1")
    max_poll_interval_ms, max_record_age_seconds = _retry_lease_settings()
    retry_backoff_seconds = _retry_backoff_seconds()
    exit_after_idle_polls = _exit_after_idle_polls()
    raw_consumer = KafkaConsumer(
        {
            "bootstrap.servers": brokers,
            "group.id": os.getenv("CONSUMER_GROUP", "pypi-reasoning-v1"),
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest",
            "max.poll.interval.ms": max_poll_interval_ms,
        }
    )
    raw_consumer.subscribe([input_topic])
    consumer = ConfluentConsumerAdapter(raw_consumer)
    producer = Producer(_producer_config(brokers))
    worker = ReasoningWorker(
        consumer,
        ConfluentAcknowledgedPublisher(producer),
        pipeline,
        telemetry=telemetry,
        findings_topic=os.getenv("FINDINGS_TOPIC", "pypi.findings.v1"),
        failures_topic=os.getenv("FAILURES_TOPIC", "pypi.failures.v1"),
        max_record_age_seconds=max_record_age_seconds,
    )
    server = _health_server()
    metrics_server = _metrics_server(telemetry)
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _Health.ready = True
    LOG.info("reasoning worker ready")
    drain_completion = (
        _IdleDrainCompletion(exit_after_idle_polls) if exit_after_idle_polls else None
    )
    try:
        while running:
            processed = _run_worker_iteration(
                worker,
                retry_backoff_seconds=retry_backoff_seconds,
            )
            if drain_completion is not None and drain_completion.observe(
                processed=processed,
                ready=_Health.ready,
            ):
                LOG.info(
                    "fixture batch drained after %d committed records",
                    drain_completion.committed_records,
                )
                break
    finally:
        _Health.ready = False
        worker.close()
        consumer.close()
        server.shutdown()
        metrics_server.shutdown()


if __name__ == "__main__":
    main()
