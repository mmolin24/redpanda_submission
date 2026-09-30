from __future__ import annotations

import pytest
from helpers import evidence, substantive

from reasoning_worker.models import Disposition
from reasoning_worker.provider import FakeModelProvider, FakeOutcome
from reasoning_worker.reasoning import MaterialityEngine, ReasoningStageFailure
from reasoning_worker.workflow import route


def routing_for(bundle):
    return route(is_prerelease=False, evidence=bundle)


def test_unknown_evidence_id_triggers_one_neutral_correction():
    bundle = evidence()
    provider = FakeModelProvider(
        [
            FakeOutcome(parsed=substantive(evidence_id="missing.fact")),
            FakeOutcome(parsed=substantive()),
        ]
    )
    outcome = MaterialityEngine(provider).evaluate(bundle, routing_for(bundle))
    assert outcome.result is not None
    assert outcome.result.confidence == 0.9
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "materiality_correction",
    ]
    assert [request.cache_namespace for request in provider.requests] == [
        "materiality",
        "materiality",
    ]
    assert provider.requests[0].instructions == provider.requests[1].instructions
    assert provider.requests[0].instruction_suffix is None
    assert provider.requests[1].instruction_suffix is not None
    correction = provider.requests[1].model_input
    assert "validation_errors" in correction
    assert "business decision" not in str(correction).lower()


def test_malformed_shape_uses_correction_then_accepts_valid_result():
    bundle = evidence()
    provider = FakeModelProvider(
        [
            FakeOutcome(parsed={"decision": "substantive"}),
            FakeOutcome(parsed=substantive()),
        ]
    )
    outcome = MaterialityEngine(provider).evaluate(bundle, routing_for(bundle))
    assert outcome.result is not None
    assert len(outcome.calls) == 2


def test_second_invalid_result_dead_letters_instead_of_looping():
    bundle = evidence()
    provider = FakeModelProvider(
        [
            FakeOutcome(parsed=substantive(evidence_id="missing.one")),
            FakeOutcome(parsed=substantive(evidence_id="missing.two")),
        ]
    )
    with pytest.raises(ReasoningStageFailure) as error:
        MaterialityEngine(provider).evaluate(bundle, routing_for(bundle))
    assert error.value.error_class == "semantic_validation_exhausted"
    assert len(provider.requests) == 2


def test_low_confidence_substantive_result_reviews_once_on_standard_max():
    bundle = evidence()
    provider = FakeModelProvider(
        [FakeOutcome(parsed=substantive(0.4)), FakeOutcome(parsed=substantive(0.55))]
    )
    outcome = MaterialityEngine(provider).evaluate(bundle, routing_for(bundle))
    assert outcome.disposition == Disposition.LOW_CONFIDENCE
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "materiality_review",
    ]
    assert provider.requests[1].cache_namespace == "materiality"
    assert provider.requests[1].instructions == provider.requests[0].instructions
    assert provider.requests[1].instruction_suffix is not None
    assert provider.requests[1].service_tier.value == "default"
    assert provider.requests[1].reasoning_effort.value == "max"


def test_refusal_is_explicit_terminal_disposition():
    bundle = evidence()
    provider = FakeModelProvider([FakeOutcome(refusal="cannot comply")])
    outcome = MaterialityEngine(provider).evaluate(bundle, routing_for(bundle))
    assert outcome.disposition == Disposition.REFUSED
    assert outcome.result is None


def test_invalid_confidence_review_uses_the_single_available_correction():
    bundle = evidence()
    provider = FakeModelProvider(
        [
            FakeOutcome(parsed=substantive(0.4)),
            FakeOutcome(parsed=substantive(0.5, evidence_id="missing.review")),
            FakeOutcome(parsed=substantive(0.8)),
        ]
    )
    outcome = MaterialityEngine(provider).evaluate(bundle, routing_for(bundle))
    assert outcome.result is not None
    assert outcome.result.confidence == 0.8
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "materiality_review",
        "materiality_correction",
    ]


def test_materiality_request_contains_only_release_evidence():
    bundle = evidence()
    provider = FakeModelProvider([FakeOutcome(parsed=substantive())])
    MaterialityEngine(provider).evaluate(bundle, routing_for(bundle))
    request = provider.requests[0].model_input
    assert set(request) == {"evidence"}
    rendered = str(request)
    assert "seed-a" not in rendered
    assert "processing_priority" not in rendered
