from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from helpers import event

from reasoning_worker.app import (
    FixtureEnricher,
    _exit_after_idle_polls,
    _fake_model_delay_seconds,
    _Health,
    _IdleDrainCompletion,
    _Metrics,
    _model_mode,
    _openai_timeout_seconds,
    _producer_config,
    _retry_backoff_seconds,
    _retry_lease_settings,
    _run_worker_iteration,
    build_pipeline,
)
from reasoning_worker.evidence import PyPIResourceNotFound
from reasoning_worker.models import Disposition, FailureRecord, Finding
from reasoning_worker.monitoring import load_monitored_packages
from reasoning_worker.provider import FakeModelProvider
from reasoning_worker.reasoning import ApplicabilityEngine, MaterialityEngine
from reasoning_worker.telemetry import WorkerTelemetry
from reasoning_worker.terminal import TERMINAL_RECORD_MAX_BYTES
from reasoning_worker.workflow import ProcessingBackpressure, ReasoningPipeline


def test_checked_in_monitoring_config_and_fake_mode_form_a_runnable_offline_pipeline(
    monkeypatch,
):
    monkeypatch.setenv("MODEL_MODE", "fake")
    monkeypatch.setenv("FIXTURE_HISTORY_PATH", "data/fixtures/package-history.json")
    monitored = load_monitored_packages(Path("config/monitored-packages.json"))
    telemetry = WorkerTelemetry()
    pipeline = build_pipeline(monitored, telemetry)
    release_event = event(package="urllib3", version="2.6.0")
    terminal = pipeline.process(release_event)
    assert isinstance(terminal, Finding)
    assert terminal.publishable is True
    assert terminal.analysis_method == "deterministic"
    assert terminal.analysis_metadata["model_calls"] == []
    assert terminal.gate_results["materiality"]["change_types"] == ["python_compatibility"]
    assert terminal.gate_results["deterministic_impact"]["model_calls_avoided"] == 3
    assert "pypi_reasoning_model_calls_total" in telemetry.metrics.render().decode()


def test_default_model_mode_uses_openai_when_a_key_is_configured(monkeypatch):
    monkeypatch.delenv("MODEL_MODE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "configured")

    assert _model_mode() == "openai"


def test_auto_model_mode_uses_fake_without_a_key(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert _model_mode() == "fake"


def test_explicit_fake_mode_overrides_a_configured_key(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "fake")
    monkeypatch.setenv("OPENAI_API_KEY", "configured")

    assert _model_mode() == "fake"


@pytest.mark.parametrize("value", ["-1", "31", "nan", "inf", "not-a-number"])
def test_fake_model_delay_rejects_invalid_or_unbounded_values(monkeypatch, value):
    monkeypatch.setenv("FAKE_MODEL_DELAY_SECONDS", value)

    with pytest.raises(ValueError, match="FAKE_MODEL_DELAY_SECONDS"):
        _fake_model_delay_seconds()


def test_fake_model_delay_is_local_only(monkeypatch):
    monkeypatch.setenv("FAKE_MODEL_DELAY_SECONDS", "1")
    monkeypatch.setenv("DEPLOYMENT_ENV", "production")

    with pytest.raises(ValueError, match="local-only"):
        _fake_model_delay_seconds()


def test_pypi_evidence_can_fail_before_the_fake_model_is_called(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "fake")
    monkeypatch.setenv("EVIDENCE_MODE", "pypi")

    def missing_release(_fetcher, _url):
        raise PyPIResourceNotFound("candidate missing")

    monkeypatch.setattr(
        "reasoning_worker.evidence.BoundedHttpJsonFetcher.fetch_json",
        missing_release,
    )
    monitored = load_monitored_packages(Path("config/monitored-packages.json"))
    pipeline = build_pipeline(monitored, WorkerTelemetry())

    terminal = pipeline.process(event(package="urllib3", version="0.0.0.post999999999"))

    assert isinstance(terminal, FailureRecord)
    assert terminal.error_class == "exact_release_not_found"
    assert terminal.payload["model_calls"] == []


def test_fixture_history_covers_seed_history_and_named_deterministic_scenarios():
    history_path = Path("data/fixtures/package-history.json")
    history = json.loads(history_path.read_text())
    entries = history["events"]
    expected_packages = {entry["package"] for entry in entries}
    scenario_manifest = json.loads(Path("data/fixtures/deterministic-scenarios.json").read_text())[
        "scenarios"
    ]

    assert history["synthetic"] is True
    assert len(entries) == 24
    package_counts = Counter(entry["package"] for entry in entries)
    for package in {
        "boto3",
        "urllib3",
        "requests",
        "setuptools",
        "certifi",
        "typing-extensions",
        "idna",
        "charset-normalizer",
        "python-dateutil",
        "six",
    }:
        assert package_counts[package] >= 2
    assert {entry.get("scenario_id") for entry in entries if entry.get("scenario_id")} == {
        scenario["id"] for scenario in scenario_manifest
    }

    by_package = defaultdict(list)
    enricher = FixtureEnricher(history_path)
    bundle_ids = set()
    for entry in entries:
        by_package[entry["package"]].append(entry)
        release_event = event(package=entry["package"], version=entry["version"])
        bundle = enricher.enrich(release_event)
        assert bundle.baseline["version"] == entry["baseline"]["version"]
        assert bundle.candidate["version"] == entry["version"]
        assert bundle.context["fixture"]["synthetic"] is True
        bundle_ids.add(bundle.bundle_id)

    assert len(bundle_ids) == 24
    for package in expected_packages:
        for first, second in zip(by_package[package], by_package[package][1:], strict=False):
            assert second["baseline"]["version"] == first["version"]


def test_every_supported_deterministic_scenario_has_an_individual_zero_model_terminal():
    scenarios = json.loads(Path("data/fixtures/deterministic-scenarios.json").read_text())[
        "scenarios"
    ]
    provider = FakeModelProvider([])
    pipeline = ReasoningPipeline(
        enricher=FixtureEnricher(Path("data/fixtures/package-history.json")),
        materiality=MaterialityEngine(provider),
        applicability=ApplicabilityEngine(provider),
    )

    assert [scenario["id"] for scenario in scenarios] == [
        "R01",
        "R02",
        "R03",
        "R04",
        "R05",
        "R06",
    ]
    for scenario in scenarios:
        _, package, version = scenario["event_key"].split(":", 2)
        terminal = pipeline.process(event(package=package, version=version))

        assert isinstance(terminal, Finding), scenario["id"]
        assert terminal.analysis_method == "deterministic", scenario["id"]
        assert terminal.analysis_metadata["model_calls"] == [], scenario["id"]
        assert terminal.disposition.value == scenario["disposition"], scenario["id"]
        assert terminal.publishable is scenario["publishable"], scenario["id"]
        assert terminal.routing["analysis_eligibility"] == scenario["routing"], scenario["id"]

        proof = scenario["proof"]
        if proof == "prerelease":
            assert terminal.disposition == Disposition.PRERELEASE
        elif proof == "non_substantive":
            assert terminal.gate_results["deterministic_triage"]["decision"] == proof
        elif proof == "support_expanded":
            assert terminal.gate_results["deterministic_impact"]["decision"] == proof
        else:
            impacts = terminal.gate_results["deterministic_impact"]["impacts"]
            assert proof in {impact["dimension"] for impact in impacts}
            if scenario["id"] == "R04":
                assert {impact["dimension"] for impact in impacts} >= {
                    "python_version",
                    "wheel_coverage",
                }

    assert provider.requests == []


def test_fixture_enricher_rejects_events_outside_the_checked_in_history():
    enricher = FixtureEnricher(Path("data/fixtures/package-history.json"))
    with pytest.raises(ValueError, match="no deterministic fixture evidence"):
        enricher.enrich(event(package="urllib3", version="99.0.0"))


def test_metrics_handler_is_wired_to_bounded_worker_metrics():
    telemetry = WorkerTelemetry()
    telemetry.record_terminal("finding", 0.125)
    _Metrics.telemetry = telemetry
    body = _Metrics.telemetry.metrics.render().decode()
    assert 'pypi_reasoning_messages_total{outcome="finding"} 1' in body
    assert "pypi_reasoning_processing_duration_seconds_sum" in body


def test_worker_remains_scrapeable_and_readiness_recovers_during_backpressure():
    class BackpressureThenSuccess:
        def __init__(self):
            self.calls = 0

        def run_once(self, _timeout):
            self.calls += 1
            if self.calls == 1:
                raise ProcessingBackpressure(
                    "materiality",
                    "openai_attempts_exhausted",
                    "provider unavailable",
                    retryable=True,
                )
            return True

    worker = BackpressureThenSuccess()
    sleeps = []
    _Health.ready = True

    assert (
        _run_worker_iteration(
            worker,
            retry_backoff_seconds=3,
            sleeper=sleeps.append,
        )
        is False
    )
    assert _Health.ready is False
    assert sleeps == [3]

    assert (
        _run_worker_iteration(
            worker,
            retry_backoff_seconds=3,
            sleeper=sleeps.append,
        )
        is True
    )
    assert _Health.ready is True


def test_retry_lease_must_end_before_consumer_max_poll_interval(monkeypatch):
    monkeypatch.setenv("CONSUMER_MAX_POLL_INTERVAL_MS", "3600000")
    monkeypatch.setenv("PROCESSING_RETRY_MAX_ELAPSED_SECONDS", "1800")
    assert _retry_lease_settings() == (3_600_000, 1_800)

    monkeypatch.setenv("CONSUMER_MAX_POLL_INTERVAL_MS", "300000")
    monkeypatch.setenv("PROCESSING_RETRY_MAX_ELAPSED_SECONDS", "239")
    assert _retry_lease_settings() == (300_000, 239)

    monkeypatch.setenv("PROCESSING_RETRY_MAX_ELAPSED_SECONDS", "240")
    with pytest.raises(ValueError, match="at least 60 seconds"):
        _retry_lease_settings()

    monkeypatch.setenv("CONSUMER_MAX_POLL_INTERVAL_MS", "3600000")
    monkeypatch.setenv("PROCESSING_RETRY_MAX_ELAPSED_SECONDS", "nan")
    with pytest.raises(ValueError, match="finite"):
        _retry_lease_settings()


def test_openai_request_timeout_is_bounded_by_the_processing_lease(monkeypatch):
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "60")
    assert _openai_timeout_seconds() == 60

    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "61")
    with pytest.raises(ValueError, match="at most 60"):
        _openai_timeout_seconds()


def test_reasoning_producer_has_explicit_headroom_above_terminal_budget():
    config = _producer_config("redpanda:9092")

    message_max_bytes = config["message.max.bytes"]
    assert isinstance(message_max_bytes, int)
    assert message_max_bytes == 1_000_000
    assert message_max_bytes - TERMINAL_RECORD_MAX_BYTES > 200 * 1024
    assert config["enable.idempotence"] is True
    assert config["acks"] == "all"
    assert config["max.in.flight.requests.per.connection"] == 1


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "60.1"])
def test_retry_backoff_rejects_hot_loops_non_finite_values_and_extremes(monkeypatch, value):
    monkeypatch.setenv("PROCESSING_RETRY_BACKOFF_SECONDS", value)
    with pytest.raises(ValueError, match=r"between 0\.1 and 60 seconds"):
        _retry_backoff_seconds()


@pytest.mark.parametrize("value", ["0.1", "5", "60"])
def test_retry_backoff_accepts_documented_boundaries(monkeypatch, value):
    monkeypatch.setenv("PROCESSING_RETRY_BACKOFF_SECONDS", value)
    assert math.isclose(_retry_backoff_seconds(), float(value))


@pytest.mark.parametrize("value", ["-1", "61", "1.5", "invalid"])
def test_finite_batch_idle_limit_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("EXIT_AFTER_IDLE_POLLS", value)
    with pytest.raises(ValueError, match="integer between 0 and 60"):
        _exit_after_idle_polls()


@pytest.mark.parametrize(("value", "expected"), [("0", 0), ("1", 1), ("60", 60)])
def test_finite_batch_idle_limit_accepts_documented_boundaries(
    monkeypatch,
    value,
    expected,
):
    monkeypatch.setenv("EXIT_AFTER_IDLE_POLLS", value)
    assert _exit_after_idle_polls() == expected


def test_finite_batch_exits_only_after_committed_work_and_consecutive_idle_polls():
    completion = _IdleDrainCompletion(required_idle_polls=2)

    assert completion.observe(processed=False, ready=True) is False
    assert completion.observe(processed=True, ready=True) is False
    assert completion.observe(processed=False, ready=True) is False
    assert completion.observe(processed=False, ready=False) is False
    assert completion.observe(processed=False, ready=True) is False
    assert completion.observe(processed=False, ready=True) is True
    assert completion.committed_records == 1
