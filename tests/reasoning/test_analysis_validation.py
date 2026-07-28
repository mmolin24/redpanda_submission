from __future__ import annotations

from dataclasses import replace

import pytest
from helpers import event, evidence

from reasoning_worker.models import (
    ApplicabilityResult,
    CustomerImpactSummary,
    Disposition,
    MaterialityResult,
    Priority,
    ReasoningEffort,
    RoutingEnvelope,
    ServiceTier,
)
from reasoning_worker.validation import (
    ground_customer_impact_summary,
    validate_applicability,
    validate_customer_impact_summary,
    validate_deterministic_analysis,
    validate_model_assisted_analysis,
    validate_publication_contract,
)


def serialization_results() -> tuple[MaterialityResult, ApplicabilityResult, CustomerImpactSummary]:
    materiality = MaterialityResult.from_dict(
        {
            "decision": "substantive",
            "change_types": ["runtime_behavior_unobservable"],
            "claims": [
                {
                    "statement": "Serialized state gains a stable cross-version representation.",
                    "evidence_ids": ["artifact.state-diff"],
                    "support": "conditional",
                    "conditions": ["When applications persist package-owned state."],
                }
            ],
            "missing_evidence": [],
            "confidence": 0.97,
        }
    )
    applicability = ApplicabilityResult.from_dict(
        {
            "assessment": "Persisted package objects can cross the upgrade boundary.",
            "consumer_scenarios": [
                {
                    "impact_kind": "runtime",
                    "package": "state-codec",
                    "candidate_version": "3.1.0",
                    "consumer_trigger": "A process serializes or deserializes package-owned objects.",
                    "changed_behavior": "The objects now use a stable serialized representation.",
                    "observable_outcome": "Objects produced by the prior release can load after the upgrade.",
                    "verification": "Serialize representative objects before upgrading, load them afterward, and compare their types and values.",
                    "evidence_ids": ["artifact.state-diff"],
                    "conditions": ["The process persists package-owned objects"],
                }
            ],
            "confidence": 0.97,
            "limitations": ["Only the accepted object types are covered."],
        }
    )
    summary = CustomerImpactSummary.from_dict(
        {
            "decision": "publishable_summary",
            "impact_type": "other",
            "headline": "state-codec 3.1.0 stabilizes serialized objects",
            "affected_if": "You persist package objects.",
            "what_happens": "The representation changes.",
            "not_affected_if": "This does not apply when package objects are never persisted.",
            "recommended_action": "Upgrade when cross-version loading is required.",
            "verification": "Print the installed package version.",
            "reach_summary": "The explicitly monitored package scope was evaluated.",
            "evidence_ids": ["artifact.state-diff"],
            "limitations": [],
        }
    )
    return materiality, applicability, summary


def test_customer_summary_is_grounded_to_accepted_runtime_scenario():
    materiality, applicability, summary = serialization_results()

    grounded = ground_customer_impact_summary(summary, applicability)

    scenario = applicability.consumer_scenarios[0]
    assert grounded.affected_if == scenario.consumer_trigger
    assert grounded.what_happens == (f"{scenario.changed_behavior} {scenario.observable_outcome}")
    assert grounded.verification == scenario.verification
    assert grounded.evidence_ids == scenario.evidence_ids
    assert grounded.limitations == applicability.limitations
    assert grounded.decision_card is not None
    assert grounded.decision_card.headline == grounded.headline
    assert grounded.decision_card.applies_when == scenario.consumer_trigger
    assert grounded.decision_card.action == grounded.recommended_action
    assert grounded.decision_card.source_scenario_id == "scenario-1"
    validation = validate_customer_impact_summary(
        grounded,
        materiality,
        applicability,
        package="state-codec",
        candidate_version="3.1.0",
    )
    assert validation.valid is True


def test_customer_summary_validator_rejects_drift_from_accepted_scenario():
    materiality, applicability, summary = serialization_results()
    grounded = ground_customer_impact_summary(summary, applicability)
    drifted = replace(grounded, verification="Print the installed package version.")

    validation = validate_customer_impact_summary(
        drifted,
        materiality,
        applicability,
        package="state-codec",
        candidate_version="3.1.0",
    )

    assert validation.valid is False
    assert "verification must preserve the accepted scenario check" in validation.errors


def test_customer_summary_validator_rejects_scenario_evidence_drift():
    materiality, applicability, summary = serialization_results()
    grounded = ground_customer_impact_summary(summary, applicability)
    drifted = replace(grounded, evidence_ids=("artifact.other",))

    validation = validate_customer_impact_summary(
        drifted,
        materiality,
        applicability,
        package="state-codec",
        candidate_version="3.1.0",
    )

    assert validation.valid is False
    assert "summary evidence must match the accepted scenario evidence" in validation.errors


def test_customer_summary_validator_rejects_decision_card_drift():
    materiality, applicability, summary = serialization_results()
    grounded = ground_customer_impact_summary(summary, applicability)
    assert grounded.decision_card is not None
    drifted = replace(
        grounded,
        decision_card=replace(grounded.decision_card, action="Upgrade without testing."),
    )

    validation = validate_customer_impact_summary(
        drifted,
        materiality,
        applicability,
        package="state-codec",
        candidate_version="3.1.0",
    )

    assert validation.valid is False
    assert "decision card action must preserve the validated recommendation" in validation.errors


def test_applicability_validator_bounds_the_decision_card_trigger():
    materiality, applicability, _ = serialization_results()
    scenario = applicability.consumer_scenarios[0]
    verbose = replace(
        applicability,
        consumer_scenarios=(
            replace(
                scenario,
                consumer_trigger=" ".join(f"condition{index}" for index in range(21)),
            ),
        ),
    )

    validation = validate_applicability(
        verbose,
        materiality,
        package="state-codec",
        candidate_version="3.1.0",
    )

    assert validation.valid is False
    assert (
        "consumer_scenarios[0].consumer_trigger must contain at most 20 words" in validation.errors
    )


@pytest.mark.parametrize(
    (
        "package",
        "candidate_version",
        "change_type",
        "impact_kind",
        "impact_type",
        "headline",
        "consumer_trigger",
        "recommended_action",
    ),
    [
        (
            "web-client",
            "5.0.0",
            "python_compatibility",
            "metadata",
            "runtime_compatibility",
            "web-client 5.0.0 drops Python 3.9",
            "A deployment installs web-client on Python 3.9.",
            "Upgrade Python before adopting this release.",
        ),
        (
            "resolver-kit",
            "4.2.0",
            "dependency_contract",
            "metadata",
            "dependency_conflict",
            "resolver-kit 4.2.0 raises dependency floor",
            "An environment pins httpcore below version 1.0.",
            "Check dependency constraints before upgrading resolver-kit.",
        ),
        (
            "wheelhouse",
            "2.1.0",
            "platform_installability",
            "metadata",
            "platform_installability",
            "wheelhouse 2.1.0 drops musllinux wheels",
            "A Linux deployment installs only musllinux wheels.",
            "Build from source or retain the prior release.",
        ),
        (
            "release-tool",
            "8.0.1",
            "release_withdrawal",
            "metadata",
            "release_withdrawal",
            "release-tool 8.0.1 is withdrawn",
            "An installer selects release-tool version 8.0.1.",
            "Pin the prior release while reviewing the withdrawal.",
        ),
        (
            "crypto-helper",
            "7.4.1",
            "security_advisory",
            "metadata",
            "security_signal",
            "crypto-helper 7.4.1 addresses GHSA-abcd-1234-5678",
            "An environment runs a version covered by GHSA-abcd-1234-5678.",
            "Confirm the installed version and follow the advisory.",
        ),
        (
            "http-client",
            "3.0.0",
            "runtime_behavior_unobservable",
            "runtime",
            "other",
            "http-client 3.0.0 changes redirect handling",
            "An application sends authenticated cross-host redirect requests.",
            "Test representative redirects before upgrading http-client.",
        ),
    ],
)
def test_decision_card_is_package_neutral_across_release_change_types(
    package: str,
    candidate_version: str,
    change_type: str,
    impact_kind: str,
    impact_type: str,
    headline: str,
    consumer_trigger: str,
    recommended_action: str,
):
    evidence_id = f"computed.{change_type}.after"
    materiality = MaterialityResult.from_dict(
        {
            "decision": "substantive",
            "change_types": [change_type],
            "claims": [
                {
                    "statement": f"{package} has an evidence-backed {change_type} change.",
                    "evidence_ids": [evidence_id],
                    "support": "conditional",
                    "conditions": ["The described release condition applies."],
                }
            ],
            "missing_evidence": [],
            "confidence": 0.9,
        }
    )
    applicability = ApplicabilityResult.from_dict(
        {
            "assessment": f"{package} has one supported release impact.",
            "consumer_scenarios": [
                {
                    "impact_kind": impact_kind,
                    "package": package,
                    "candidate_version": candidate_version,
                    "consumer_trigger": consumer_trigger,
                    "changed_behavior": f"The accepted {change_type} behavior changed.",
                    "observable_outcome": "The described release outcome is observable.",
                    "verification": f"Check the {package} release evidence before upgrading.",
                    "evidence_ids": [evidence_id],
                    "conditions": ["The described release condition applies"],
                }
            ],
            "confidence": 0.9,
            "limitations": ["Only the described release evidence and trigger are covered."],
        }
    )
    summary = CustomerImpactSummary.from_dict(
        {
            "decision": "publishable_summary",
            "impact_type": impact_type,
            "headline": headline,
            "affected_if": "This model-authored value is replaced.",
            "what_happens": "This model-authored value is replaced.",
            "not_affected_if": "This impact does not apply outside the stated trigger.",
            "recommended_action": recommended_action,
            "verification": "This model-authored value is replaced.",
            "reach_summary": "The explicitly monitored package scope was evaluated.",
            "evidence_ids": ["model-authored-id"],
            "limitations": [],
        }
    )

    applicability_validation = validate_applicability(
        applicability,
        materiality,
        package=package,
        candidate_version=candidate_version,
    )
    grounded = ground_customer_impact_summary(summary, applicability)
    summary_validation = validate_customer_impact_summary(
        grounded,
        materiality,
        applicability,
        package=package,
        candidate_version=candidate_version,
    )

    assert applicability_validation.valid is True
    assert summary_validation.valid is True
    assert grounded.decision_card is not None
    assert grounded.decision_card.headline == headline
    assert grounded.decision_card.applies_when == consumer_trigger
    assert grounded.decision_card.action == recommended_action
    assert grounded.decision_card.source_scenario_id == "scenario-1"
    assert grounded.evidence_ids == (evidence_id,)


def test_deterministic_validator_requires_versioned_reproducible_proof():
    release_event = event()
    bundle = evidence(release_event)
    routing = RoutingEnvelope(
        processing_priority=Priority.SKIP,
        analysis_eligibility="deterministic_non_substantive",
        reasoning_complexity="simple",
        reasons=("no_scoped_release_change",),
        service_tier=None,
        model=None,
        reasoning_effort=None,
    )

    result = validate_deterministic_analysis(
        routing=routing,
        disposition=Disposition.NON_SUBSTANTIVE,
        gate_results={"deterministic_triage": {"decision": "non_substantive"}},
        model_calls=[],
        evidence=bundle,
    )

    assert result.valid is False
    assert any("rule_id" in error for error in result.errors)
    assert any("evidence_bundle_id" in error for error in result.errors)


def test_model_assisted_validator_rejects_missing_model_lineage():
    routing = RoutingEnvelope(
        processing_priority=Priority.MEDIUM,
        analysis_eligibility="model",
        reasoning_complexity="moderate",
        reasons=("explicitly_monitored_package",),
        service_tier=ServiceTier.FLEX,
        model="gpt-5.6-sol",
        reasoning_effort=ReasoningEffort.LOW,
    )

    result = validate_model_assisted_analysis(
        routing=routing,
        disposition=Disposition.PUBLISHABLE,
        gate_results={"materiality": {}},
        model_calls=[],
    )

    assert result.valid is False
    assert "model-assisted analysis requires at least one model call" in result.errors
    assert "publishable model-assisted analysis requires valid customer impact" in result.errors


def test_shared_publication_contract_rejects_identity_drift():
    release_event = event()
    result = validate_publication_contract(
        analysis_method="deterministic",
        event_key=release_event.event_key,
        package=release_event.package.normalized_name,
        candidate_version="unexpected-version",
        disposition=Disposition.NON_SUBSTANTIVE,
        evidence=evidence(release_event),
        path_validation=validate_model_assisted_analysis(
            routing=RoutingEnvelope(
                processing_priority=Priority.MEDIUM,
                analysis_eligibility="model",
                reasoning_complexity="moderate",
                reasons=("test",),
                service_tier=ServiceTier.FLEX,
                model="gpt-5.6-sol",
                reasoning_effort=ReasoningEffort.LOW,
            ),
            disposition=Disposition.NON_SUBSTANTIVE,
            gate_results={"materiality": {}},
            model_calls=[],
        ),
    )

    assert result.valid is False
    assert "evidence candidate version does not match the terminal candidate" in result.errors
