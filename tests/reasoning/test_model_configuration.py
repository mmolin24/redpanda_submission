from pathlib import Path

import pytest
from helpers import applicability, customer_impact, event, evidence, substantive

from reasoning_worker.app import build_pipeline
from reasoning_worker.models import Finding, TokenUsage
from reasoning_worker.monitoring import load_monitored_packages
from reasoning_worker.provider import FakeModelProvider, FakeOutcome, _estimate_cost
from reasoning_worker.telemetry import WorkerTelemetry
from reasoning_worker.workflow import StaticEnricher


def test_configured_model_and_effort_reach_every_stage_and_correction(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "fake")
    monkeypatch.setenv("FIXTURE_HISTORY_PATH", "data/fixtures/package-history.json")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    monkeypatch.setenv("OPENAI_SERVICE_TIER", "flex")
    worker = build_pipeline(
        load_monitored_packages(Path("config/monitored-packages.json")), WorkerTelemetry()
    )
    bad_materiality = substantive(evidence_id="not-an-evidence-id")
    bad_applicability = applicability()
    bad_applicability["consumer_scenarios"][0]["package"] = "candidate"
    bad_summary = customer_impact()
    bad_summary["headline"] = "Consumers selecting the new release"
    provider = FakeModelProvider(
        [
            FakeOutcome(parsed=value)
            for value in [
                bad_materiality,
                substantive(confidence=0.5),
                substantive(),
                bad_applicability,
                applicability(),
                bad_summary,
                customer_impact(),
            ]
        ]
    )
    worker.monitored_packages = None
    worker.enricher = StaticEnricher(evidence(event()))
    for engine in [worker.materiality, worker.applicability, worker.customer_impact]:
        engine.provider = provider
    terminal = worker.process(event())
    assert isinstance(terminal, Finding)
    assert terminal.publishable
    assert [r.purpose for r in provider.requests] == [
        "materiality_assessment",
        "materiality_correction",
        "materiality_review",
        "applicability_assessment",
        "applicability_correction",
        "customer_impact_summary",
        "customer_impact_correction",
    ]
    assert all(r.model == "gpt-6.1-sol" for r in provider.requests)
    assert all(r.reasoning_effort.value == "high" for r in provider.requests)
    assert all(r.service_tier.value == "flex" for r in provider.requests)
    settings = terminal.analysis_metadata["versions"]["model_execution_settings"]
    assert settings["model"] == "gpt-6.1-sol"
    assert settings["reasoning_effort"] == "high"


@pytest.mark.parametrize(("tier", "expected"), [("flex", 0.1269), ("default", 0.2538)])
def test_sol61_estimate_accounts_for_cache_classes_and_reasoning(tier, expected):
    usage = TokenUsage(
        input_tokens=100000,
        cached_input_tokens=20000,
        cache_write_tokens=30000,
        output_tokens=7680,
        reasoning_tokens=5000,
    )
    assert _estimate_cost(usage, model="gpt-6.1-sol", service_tier=tier) == expected


def test_sol61_long_context_applies_full_request_multipliers():
    usage = TokenUsage(input_tokens=300000, output_tokens=10000)
    assert _estimate_cost(usage, model="gpt-6.1-sol", service_tier="flex") == 0.675


def test_configured_model_without_pricing_is_rejected_before_execution(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "fake")
    monkeypatch.setenv("FIXTURE_HISTORY_PATH", "data/fixtures/package-history.json")
    monkeypatch.setenv("OPENAI_MODEL", "unpriced-model")
    with pytest.raises(ValueError, match="no price table entry"):
        build_pipeline(
            load_monitored_packages(Path("config/monitored-packages.json")), WorkerTelemetry()
        )


def test_unpriced_model_never_receives_an_unrelated_cost_estimate():
    with pytest.raises(ValueError, match="no price table entry"):
        _estimate_cost(
            TokenUsage(input_tokens=1_000_000), model="unpriced-model", service_tier="flex"
        )
