"""Exercise deterministic and model-assisted reasoning workflow behavior."""

from __future__ import annotations

import pytest
from helpers import (
    applicability,
    customer_impact,
    event,
    evidence,
    substantive,
)

from reasoning_worker.evidence import ExactReleaseNotFound, MetadataEvidenceBuilder
from reasoning_worker.model_input import ModelInputTooLarge
from reasoning_worker.models import (
    ApplicabilityResult,
    Disposition,
    EvidenceCollectionStatus,
    FailureRecord,
    Finding,
    MaterialityResult,
    ModelCallRecord,
    PhysicalAttempt,
    TokenUsage,
    TraceContext,
    utc_now,
)
from reasoning_worker.provider import (
    FakeModelProvider,
    FakeOutcome,
    ProviderExhausted,
    ProviderIncomplete,
)
from reasoning_worker.reasoning import (
    APPLICABILITY_CORRECTION_INSTRUCTIONS,
    APPLICABILITY_INSTRUCTIONS,
    CUSTOMER_IMPACT_CORRECTION_INSTRUCTIONS,
    CUSTOMER_IMPACT_INSTRUCTIONS,
    MATERIALITY_CORRECTION_INSTRUCTIONS,
    MATERIALITY_INSTRUCTIONS,
    MATERIALITY_REVIEW_INSTRUCTIONS,
    ApplicabilityEngine,
    MaterialityEngine,
)
from reasoning_worker.validation import validate_applicability
from reasoning_worker.workflow import (
    ProcessingBackpressure,
    ReasoningPipeline,
    StaticEnricher,
)


def pipeline(release_event, outcomes):
    provider = FakeModelProvider(outcomes)
    return (
        ReasoningPipeline(
            enricher=StaticEnricher(evidence(release_event)),
            materiality=MaterialityEngine(provider),
            applicability=ApplicabilityEngine(provider),
        ),
        provider,
    )


def pipeline_with_evidence(release_event, bundle, outcomes):
    provider = FakeModelProvider(outcomes)
    return (
        ReasoningPipeline(
            enricher=StaticEnricher(bundle),
            materiality=MaterialityEngine(provider),
            applicability=ApplicabilityEngine(provider),
        ),
        provider,
    )


def unchanged_evidence(
    release_event,
    *,
    context=None,
    collection_status: EvidenceCollectionStatus = "complete",
    candidate_filename: str | None = None,
):
    common = {
        "name": release_event.package.normalized_name,
        "requires_python": ">=3.10",
        "requires_dist": ["urllib3>=2"],
        "classifiers": ["Programming Language :: Python :: 3"],
    }
    baseline = {
        "info": {**common, "version": "1.9.0"},
        "urls": [
            {
                "filename": "dependency_b-1.9.0-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "python_version": "py3",
            }
        ],
        "vulnerabilities": [],
    }
    candidate = {
        "info": {**common, "version": release_event.release.version},
        "urls": [
            {
                "filename": candidate_filename
                or f"dependency_b-{release_event.release.version}-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "python_version": "py3",
            }
        ],
        "vulnerabilities": [],
    }
    return MetadataEvidenceBuilder().build(
        release_event,
        baseline,
        candidate,
        context=context or {"repository_mapping_confidence": "unavailable"},
        collection_status=collection_status,
    )


def test_golden_substantive_case_reaches_applicability_and_publishable_customer_impact():
    release_event = event()
    worker, provider = pipeline(
        release_event,
        [
            FakeOutcome(parsed=substantive()),
            FakeOutcome(parsed=applicability()),
            FakeOutcome(parsed=customer_impact()),
        ],
    )
    terminal = worker.process(
        release_event,
        trace_context=TraceContext("00-11111111111111111111111111111111-2222222222222222-01"),
    )
    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.PUBLISHABLE
    assert terminal.publishable is True
    assert terminal.analysis_method == "model_assisted"
    assert terminal.gate_results["analysis_validation"]["path"]["valid"] is True
    assert terminal.observability["analysis_trace_id"] == "11111111111111111111111111111111"
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "applicability_assessment",
        "customer_impact_summary",
    ]
    assert [request.cache_namespace for request in provider.requests] == [
        "materiality",
        "applicability",
        "customer_impact",
    ]
    assert provider.requests[0].output_schema["properties"]["claims"]["maxItems"] == 4
    assert provider.requests[1].model_input["monitoring_context"]["directly_monitored"] is True
    assert provider.requests[1].max_output_tokens == 2400
    assert provider.requests[1].output_schema["properties"]["consumer_scenarios"]["maxItems"] == 1
    scenario_schema = provider.requests[1].output_schema["properties"]["consumer_scenarios"]
    assert scenario_schema["minItems"] == scenario_schema["maxItems"] == 1
    assert scenario_schema["items"]["properties"]["impact_kind"]["enum"] == [
        "metadata",
        "runtime",
    ]
    assert scenario_schema["items"]["properties"]["evidence_ids"]["items"]["enum"] == [
        "computed.requires_python_diff.after"
    ]
    assert len(terminal.analysis_metadata["model_calls"]) == 3
    assert terminal.gate_results["customer_impact"]["valid"] is True
    assert terminal.gate_results["customer_impact"]["customer_summary"]["headline"] == (
        "Python 3.9 blocks dependency-b 2.0.0"
    )
    assert provider.requests[2].model == "gpt-5.6-terra"
    assert provider.requests[2].max_output_tokens == 2400
    assert "observability" not in provider.requests[2].model_input
    assert terminal.gate_results["versions"]["customer_impact_policy_version"] == (
        "customer-impact-summary-v5"
    )


def test_complete_no_scoped_change_terminates_without_a_model_call():
    release_event = event()
    worker, provider = pipeline_with_evidence(
        release_event,
        unchanged_evidence(release_event),
        [],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.NON_SUBSTANTIVE
    assert terminal.publishable is False
    assert terminal.analysis_method == "deterministic"
    assert provider.requests == []
    assert terminal.routing["analysis_eligibility"] == "deterministic_non_substantive"
    assert terminal.routing["processing_priority"] == "skip"
    assert terminal.gate_results["deterministic_triage"] == {
        "decision": "non_substantive",
        "rule_id": "no-scoped-release-change",
        "rule_version": "deterministic-triage-v1",
        "evidence_bundle_id": terminal.evidence_bundle["bundle_id"],
        "reasons": [
            "explicitly_monitored_package",
            "complete_evidence",
            "no_scoped_release_change",
        ],
        "model_calls_avoided": 1,
    }
    assert terminal.gate_results["analysis_validation"] == {
        "policy_version": "analysis-validation-v5",
        "analysis_method": "deterministic",
        "path": {"valid": True, "errors": []},
        "publication": {"valid": True, "errors": []},
    }
    assert terminal.analysis_metadata["model_calls"] == []


def test_policy_revision_creates_a_new_zero_model_analysis_identity():
    release_event = event()
    bundle = unchanged_evidence(release_event)
    provider = FakeModelProvider([])
    common = {
        "enricher": StaticEnricher(bundle),
        "materiality": MaterialityEngine(provider),
        "applicability": ApplicabilityEngine(provider),
    }

    first = ReasoningPipeline(**common, analysis_policy_revision="analysis-policy-v1").process(
        release_event
    )
    second = ReasoningPipeline(**common, analysis_policy_revision="analysis-policy-v2").process(
        release_event
    )

    assert isinstance(first, Finding)
    assert isinstance(second, Finding)
    assert first.finding_id != second.finding_id
    assert (
        first.analysis_metadata["analysis_version"] != second.analysis_metadata["analysis_version"]
    )
    assert first.analysis_metadata["versions"]["analysis_policy_revision"] == "analysis-policy-v1"
    assert second.analysis_metadata["versions"]["analysis_policy_revision"] == "analysis-policy-v2"
    assert first.analysis_metadata["model_calls"] == second.analysis_metadata["model_calls"] == []


@pytest.mark.parametrize("revision", ["", "UPPERCASE", "space separated", "x" * 65])
def test_policy_revision_rejects_unstable_identifiers(revision):
    release_event = event()
    provider = FakeModelProvider([])

    with pytest.raises(ValueError, match="policy revision"):
        ReasoningPipeline(
            enricher=StaticEnricher(unchanged_evidence(release_event)),
            materiality=MaterialityEngine(provider),
            applicability=ApplicabilityEngine(provider),
            analysis_policy_revision=revision,
        )


def test_runtime_behavior_uses_reasoning_validation_and_reaches_publication():
    release_event = event()
    bundle = evidence(release_event)
    evidence_id = "context.documents.0.text"
    worker, provider = pipeline_with_evidence(
        release_event,
        bundle,
        [
            FakeOutcome(
                parsed={
                    "decision": "substantive",
                    "change_types": ["runtime_behavior_unobservable"],
                    "claims": [
                        {
                            "statement": "Cross-host redirects remove configured headers.",
                            "evidence_ids": [evidence_id],
                            "support": "conditional",
                            "conditions": ["Redirect host differs"],
                        }
                    ],
                    "missing_evidence": [],
                    "confidence": 0.9,
                }
            ),
            FakeOutcome(
                parsed={
                    "assessment": "dependency-b 2.0.0 changes cross-host redirect headers.",
                    "consumer_scenarios": [
                        {
                            "impact_kind": "runtime",
                            "package": "dependency-b",
                            "candidate_version": "2.0.0",
                            "consumer_trigger": (
                                "An application follows a different-host redirect with configured "
                                "headers."
                            ),
                            "changed_behavior": (
                                "Retry.remove_headers_on_redirect is non-empty and applies during "
                                "cross-host redirect handling."
                            ),
                            "observable_outcome": "The configured headers are removed.",
                            "verification": (
                                "Run pytest for a cross-host redirect and inspect request headers."
                            ),
                            "evidence_ids": [evidence_id],
                            "conditions": ["Redirect host != original host"],
                        }
                    ],
                    "confidence": 0.9,
                    "limitations": ["Configured redirect behavior only"],
                }
            ),
            FakeOutcome(
                parsed={
                    "decision": "publishable_summary",
                    "impact_type": "other",
                    "headline": "dependency-b 2.0.0 strips redirect headers",
                    "affected_if": (
                        "You use dependency-b 2.0.0 for a different-host redirect with "
                        "Retry.remove_headers_on_redirect configured."
                    ),
                    "what_happens": (
                        "dependency-b 2.0.0 removes configured headers because "
                        "Retry.remove_headers_on_redirect applies to a different host."
                    ),
                    "not_affected_if": "Your requests do not follow cross-host redirects.",
                    "recommended_action": "Review headers on cross-host redirects before upgrading.",
                    "verification": (
                        "Run pytest for a cross-host redirect and inspect request headers."
                    ),
                    "reach_summary": "This package is explicitly monitored.",
                    "evidence_ids": [evidence_id],
                    "limitations": ["Configured redirect behavior only"],
                }
            ),
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.PUBLISHABLE
    assert terminal.analysis_method == "model_assisted"
    assert terminal.gate_results["customer_impact"]["valid"] is True
    assert terminal.gate_results["analysis_validation"]["path"]["valid"] is True
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "applicability_assessment",
        "customer_impact_summary",
    ]


def test_changed_release_document_preserves_the_reasoning_path():
    release_event = event()
    bundle = unchanged_evidence(
        release_event,
        context={
            "documents": [
                {
                    "path": "CHANGELOG.md",
                    "before": "Previous retry behavior.",
                    "after": "HTTP 429 responses now honor Retry-After by default.",
                }
            ]
        },
    )
    worker, provider = pipeline_with_evidence(
        release_event,
        bundle,
        [
            FakeOutcome(
                parsed={
                    "decision": "non_substantive",
                    "change_types": [],
                    "claims": [],
                    "missing_evidence": [],
                    "confidence": 1.0,
                }
            )
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.NON_SUBSTANTIVE
    assert [request.purpose for request in provider.requests] == ["materiality_assessment"]
    assert terminal.routing["analysis_eligibility"] == "model"


def test_partial_evidence_preserves_the_reasoning_path():
    release_event = event()
    worker, provider = pipeline_with_evidence(
        release_event,
        unchanged_evidence(release_event, collection_status="partial"),
        [
            FakeOutcome(
                parsed={
                    "decision": "non_substantive",
                    "change_types": [],
                    "claims": [],
                    "missing_evidence": [],
                    "confidence": 1.0,
                }
            )
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert [request.purpose for request in provider.requests] == ["materiality_assessment"]
    assert terminal.routing["analysis_eligibility"] == "model"


def test_changed_wheel_compatibility_surface_publishes_without_a_model_call():
    release_event = event()
    worker, provider = pipeline_with_evidence(
        release_event,
        unchanged_evidence(
            release_event,
            candidate_filename="dependency_b-2.0.0-cp312-cp312-manylinux_2_17_x86_64.whl",
        ),
        [],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert provider.requests == []
    assert terminal.routing["analysis_eligibility"] == "deterministic_impact"
    assert terminal.analysis_method == "deterministic"
    assert terminal.disposition == Disposition.PUBLISHABLE
    assert terminal.gate_results["deterministic_impact"]["decision"] == "impact_detected"


def test_reasoning_stage_instructions_stay_within_the_cost_budget():
    assert len(MATERIALITY_INSTRUCTIONS) <= 950
    assert len(MATERIALITY_CORRECTION_INSTRUCTIONS) <= 180
    assert len(MATERIALITY_REVIEW_INSTRUCTIONS) <= 180
    assert len(APPLICABILITY_INSTRUCTIONS) <= 2_400
    assert len(APPLICABILITY_CORRECTION_INSTRUCTIONS) <= 320
    assert len(CUSTOMER_IMPACT_INSTRUCTIONS) <= 2_600
    assert len(CUSTOMER_IMPACT_CORRECTION_INSTRUCTIONS) <= 250


def test_reasoning_stage_prompts_lead_with_the_customer_decision_goal():
    assert MATERIALITY_INSTRUCTIONS.startswith("End goal:")
    assert "impact reasoning" in MATERIALITY_INSTRUCTIONS
    assert APPLICABILITY_INSTRUCTIONS.startswith("End goal:")
    assert "action or environment" in APPLICABILITY_INSTRUCTIONS
    assert "structured" in APPLICABILITY_INSTRUCTIONS
    assert "impact_kind" in APPLICABILITY_INSTRUCTIONS
    assert CUSTOMER_IMPACT_INSTRUCTIONS.startswith("End goal:")


def test_customer_impact_summary_gets_one_bounded_correction():
    release_event = event()
    invalid = customer_impact()
    invalid["headline"] = "Consumers selecting the new release"
    worker, provider = pipeline(
        release_event,
        [
            FakeOutcome(parsed=substantive()),
            FakeOutcome(parsed=applicability()),
            FakeOutcome(parsed=invalid),
            FakeOutcome(parsed=customer_impact()),
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.publishable is True
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "applicability_assessment",
        "customer_impact_summary",
        "customer_impact_correction",
    ]
    assert provider.requests[-2].cache_namespace == "customer_impact"
    assert provider.requests[-1].cache_namespace == "customer_impact"
    assert provider.requests[-2].instructions == provider.requests[-1].instructions
    assert provider.requests[-2].instruction_suffix is None
    assert provider.requests[-1].instruction_suffix is not None
    correction = provider.requests[-1].model_input
    assert (
        "headline must name the package and exact candidate version"
        in correction["validation_errors"]
    )
    assert correction["invalid_response"]["headline"] == "Consumers selecting the new release"


def test_customer_impact_natural_selection_and_python_version_need_no_correction():
    release_event = event()
    summary = customer_impact()
    summary.update(
        {
            "impact_type": "install_block",
            "headline": "dependency-b 2.0.0 drops Python 3.9 support",
            "what_happens": (
                "Requires-Python changes from >=3.9 to >=3.10, so an installer will not "
                "select dependency-b 2.0.0 on Python 3.9."
            ),
            "verification": "Run python --version before resolving dependency-b==2.0.0.",
        }
    )
    worker, provider = pipeline(
        release_event,
        [
            FakeOutcome(parsed=substantive()),
            FakeOutcome(parsed=applicability()),
            FakeOutcome(parsed=summary),
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.publishable is True
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "applicability_assessment",
        "customer_impact_summary",
    ]


def test_connect_relevance_stage_is_preserved_before_worker_stages():
    release_event = event("2.0.0rc1")
    release_event.observability["stage_summary"] = [
        {
            "stage": "relevance",
            "outcome": "completed",
            "started_at": "2026-07-20T12:35:10Z",
            "completed_at": "2026-07-20T12:35:10Z",
            "attempt": 1,
            "detail": "monitored_package",
        }
    ]
    worker, _ = pipeline(release_event, [])
    terminal = worker.process(release_event)
    assert terminal.observability["stage_summary"][0]["stage"] == "relevance"
    assert terminal.observability["stage_summary"][1]["stage"] == "enrichment"


class ExhaustedProvider:
    def complete(self, request):
        now = utc_now()
        call = ModelCallRecord(
            model_call_id="call-1",
            purpose=request.purpose,
            requested_model=request.model,
            returned_model=None,
            requested_service_tier=request.service_tier.value,
            returned_service_tier=None,
            reasoning_effort=request.reasoning_effort.value,
            request_payload=request.persisted_payload(request.compiled_input()),
            response_payload=None,
            request_sha256="sha256:request",
            response_sha256=None,
            usage=TokenUsage(),
            attempts=(PhysicalAttempt("attempt-1", "attempt-1", 1, "error", now, now, 1),),
            outcome="error",
            estimated_cost_usd=0,
        )
        raise ProviderExhausted("exhausted", call, retryable=True)


class IncompleteProvider(ExhaustedProvider):
    def complete(self, request):
        try:
            super().complete(request)
        except ProviderExhausted as exc:
            raise ProviderIncomplete("incomplete", exc.call) from exc


def test_materiality_provider_exhaustion_backpressures_without_a_terminal_record():
    release_event = event()
    worker = ReasoningPipeline(
        enricher=StaticEnricher(evidence(release_event)),
        materiality=MaterialityEngine(ExhaustedProvider()),
        applicability=ApplicabilityEngine(ExhaustedProvider()),
    )
    with pytest.raises(ProcessingBackpressure) as error:
        worker.process(release_event)

    assert error.value.stage == "materiality"
    assert error.value.error_class == "openai_attempts_exhausted"
    assert error.value.retryable is True


def test_missing_exact_release_is_a_permanent_record_failure():
    release_event = event()

    class MissingReleaseEnricher:
        def enrich(self, _event):
            raise ExactReleaseNotFound("exact release was deleted")

    worker = ReasoningPipeline(
        enricher=MissingReleaseEnricher(),
        materiality=MaterialityEngine(ExhaustedProvider()),
        applicability=ApplicabilityEngine(ExhaustedProvider()),
    )

    terminal = worker.process(release_event)
    repeated = worker.process(release_event)

    assert isinstance(terminal, FailureRecord)
    assert isinstance(repeated, FailureRecord)
    assert terminal.stage == "enrichment"
    assert terminal.error_class == "exact_release_not_found"
    assert terminal.retryable is False
    assert terminal.failure_id != repeated.failure_id
    assert terminal.failure_fingerprint == repeated.failure_fingerprint


def test_incomplete_provider_response_backpressures_without_a_terminal_record():
    release_event = event()
    worker = ReasoningPipeline(
        enricher=StaticEnricher(evidence(release_event)),
        materiality=MaterialityEngine(IncompleteProvider()),
        applicability=ApplicabilityEngine(IncompleteProvider()),
    )

    with pytest.raises(ProcessingBackpressure) as error:
        worker.process(release_event)

    assert error.value.stage == "materiality"
    assert error.value.error_class == "openai_incomplete_response"
    assert error.value.retryable is True


def test_model_input_overflow_is_nonterminal_processing_backpressure():
    release_event = event()

    class OverbudgetProvider:
        def complete(self, _request):
            raise ModelInputTooLarge("model input exceeds its aggregate budget")

    worker = ReasoningPipeline(
        enricher=StaticEnricher(evidence(release_event)),
        materiality=MaterialityEngine(OverbudgetProvider()),
        applicability=ApplicabilityEngine(OverbudgetProvider()),
    )

    with pytest.raises(ProcessingBackpressure) as error:
        worker.process(release_event)

    assert error.value.stage == "materiality"
    assert error.value.error_class == "model_input_budget_exceeded"
    assert error.value.retryable is False


def test_applicability_identity_mismatch_gets_one_bounded_correction():
    release_event = event()
    vague_applicability = applicability()
    vague_applicability["consumer_scenarios"][0]["package"] = "candidate"
    worker, provider = pipeline(
        release_event,
        [
            FakeOutcome(parsed=substantive()),
            FakeOutcome(parsed=vague_applicability),
            FakeOutcome(parsed=applicability()),
            FakeOutcome(parsed=customer_impact()),
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.PUBLISHABLE
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "applicability_assessment",
        "applicability_correction",
        "customer_impact_summary",
    ]
    correction = provider.requests[2]
    assert correction.cache_namespace == "applicability"
    assert correction.instruction_suffix == APPLICABILITY_CORRECTION_INSTRUCTIONS
    assert (
        "consumer_scenarios[0].package must equal dependency-b"
        in correction.model_input["validation_errors"]
    )


def test_applicability_accepts_atomic_scenario_conditions():
    release_event = event()
    result = applicability()
    result["consumer_scenarios"][0]["conditions"] = ["Python == 3.9"]
    worker, provider = pipeline(
        release_event,
        [
            FakeOutcome(parsed=substantive()),
            FakeOutcome(parsed=result),
            FakeOutcome(parsed=customer_impact()),
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.PUBLISHABLE
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "applicability_assessment",
        "customer_impact_summary",
    ]


def test_applicability_suppresses_after_one_failed_structured_correction():
    release_event = event()
    result = applicability()
    result["consumer_scenarios"][0]["changed_behavior"] = ""
    worker, provider = pipeline(
        release_event,
        [
            FakeOutcome(parsed=substantive()),
            FakeOutcome(parsed=result),
            FakeOutcome(parsed=result),
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.VALIDATION_FAILURE
    assert "consumer_scenarios[0].changed_behavior must not be empty" in str(
        terminal.gate_results["customer_impact"]["errors"]
    )
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "applicability_assessment",
        "applicability_correction",
    ]


def test_mixed_materiality_accepts_an_explicit_runtime_case_without_metadata_wording():
    release_event = event()
    mixed_materiality = substantive()
    mixed_materiality["change_types"] = [
        "dependency_contract",
        "runtime_behavior_unobservable",
    ]
    runtime_case = applicability()
    runtime_case["consumer_scenarios"][0].update(
        {
            "impact_kind": "runtime",
            "consumer_trigger": "A file wrapper proxies __iter__ through __getattr__.",
            "changed_behavior": (
                "PreparedRequest.prepare_body now recognizes the proxied __iter__ attribute."
            ),
            "observable_outcome": "The request body remains streamable across a 307 redirect.",
            "conditions": ["isinstance(body, Iterable) == false"],
            "verification": "POST the wrapper through a 307 redirect and compare the body.",
        }
    )
    worker, provider = pipeline(
        release_event,
        [
            FakeOutcome(parsed=mixed_materiality),
            FakeOutcome(parsed=runtime_case),
            FakeOutcome(parsed=customer_impact()),
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.PUBLISHABLE
    assert [request.purpose for request in provider.requests] == [
        "materiality_assessment",
        "applicability_assessment",
        "customer_impact_summary",
    ]


def test_requests_2341_structured_runtime_case_avoids_the_observed_lexical_false_negative():
    evidence_id = "artifact.text-diff.requests-prepare-body"
    materiality = MaterialityResult.from_dict(
        {
            "decision": "substantive",
            "change_types": ["dependency_contract", "runtime_behavior_unobservable"],
            "claims": [
                {
                    "statement": (
                        "Request bodies exposing __iter__ through __getattr__ are now detected "
                        "as iterable."
                    ),
                    "evidence_ids": [evidence_id],
                    "support": "conditional",
                    "conditions": ["isinstance(body, Iterable) is false"],
                }
            ],
            "missing_evidence": [],
            "confidence": 0.98,
        }
    )
    result = ApplicabilityResult.from_dict(
        {
            "assessment": "requests 2.34.1 changes proxied request-body stream detection.",
            "consumer_scenarios": [
                {
                    "impact_kind": "runtime",
                    "package": "requests",
                    "candidate_version": "2.34.1",
                    "consumer_trigger": (
                        "A file-like request body exposes __iter__ through __getattr__ while "
                        "isinstance(body, Iterable) is false."
                    ),
                    "changed_behavior": (
                        "PreparedRequest.prepare_body now checks hasattr(data, '__iter__') in "
                        "addition to isinstance(data, Iterable)."
                    ),
                    "observable_outcome": (
                        "The proxied stream body remains available when a 307 redirect is followed."
                    ),
                    "verification": (
                        "POST an AttrProxy body through a 307 redirect and compare the received data."
                    ),
                    "evidence_ids": [evidence_id],
                    "conditions": ["Body proxies __iter__ through __getattr__"],
                }
            ],
            "confidence": 0.98,
            "limitations": ["The evidence does not establish an observed application incident."],
        }
    )

    validation = validate_applicability(
        result,
        materiality,
        package="requests",
        candidate_version="2.34.1",
    )

    assert validation.valid, validation.errors


def test_applicability_organization_context_is_suppressed():
    release_event = event()
    result = applicability()
    result["limitations"].append("No organization-specific installation inventory is available.")
    worker, _ = pipeline(
        release_event,
        [
            FakeOutcome(parsed=substantive()),
            FakeOutcome(parsed=result),
            FakeOutcome(parsed=result),
        ],
    )

    terminal = worker.process(release_event)

    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.VALIDATION_FAILURE
    assert "contains organization-specific context" in str(
        terminal.gate_results["customer_impact"]["errors"]
    )


def test_prerelease_observed_without_model_call():
    release_event = event("2.0.0rc1")
    worker, provider = pipeline(release_event, [])
    terminal = worker.process(release_event)
    assert isinstance(terminal, Finding)
    assert terminal.disposition == Disposition.PRERELEASE
    assert provider.requests == []


def test_full_model_visible_request_and_response_are_persisted_but_not_hidden_reasoning(
    monkeypatch,
):
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.setenv("OBS_CAPTURE_MODEL_PAYLOADS", "true")
    release_event = event()
    worker, _ = pipeline(
        release_event,
        [
            FakeOutcome(parsed=substantive()),
            FakeOutcome(parsed=applicability()),
            FakeOutcome(parsed=customer_impact()),
        ],
    )
    terminal = worker.process(release_event)
    assert isinstance(terminal, Finding)
    calls = terminal.analysis_metadata["model_calls"]
    assert calls[0]["request_payload"]["input"]["evidence"]["facts"]
    assert calls[0]["response_payload"]["parsed"]["decision"] == "substantive"
    assert "hidden_reasoning" not in str(calls)
    assert "sk-secret" not in str(calls)
