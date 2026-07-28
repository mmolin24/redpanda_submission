"""Validate deterministic and model-assisted conclusions against evidence."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .deterministic_impact import RULE_VERSION
from .models import (
    CHANGE_TYPES,
    IMPACT_TYPES,
    AnalysisMethod,
    ApplicabilityResult,
    ConsumerScenario,
    CustomerImpactSummary,
    Decision,
    DecisionCard,
    Disposition,
    EvidenceBundle,
    Json,
    MaterialityResult,
    ModelCallRecord,
    RoutingEnvelope,
)

ANALYSIS_VALIDATION_POLICY_VERSION = "analysis-validation-v5"

_DECISION_CARD_SCENARIO_ID = "scenario-1"

_FORBIDDEN_SCOPE = re.compile(r"\b(downloads?|popular(?:ity)?|ranked?)\b", re.I)
_UNSUPPORTED_ABSOLUTE = re.compile(
    r"\b(will break all|breaks every|all users|guaranteed safe|is malware|is compromised)\b",
    re.I,
)
_FORBIDDEN_ORGANIZATION_CONTEXT = re.compile(
    r"\b(customer|organization-specific|installation inventory|asset-specific|"
    r"missing (?:organization )?(?:context|profile))\b",
    re.IGNORECASE,
)

ImpactCaseKind = Literal["metadata", "runtime"]

_CAUSAL_LANGUAGE = re.compile(r"\b(because|since|due to)\b", re.IGNORECASE)
_CONSUMER_ACTION = re.compile(
    r"\b(?:attempt(?:s|ed|ing)?|install(?:s|ed|ing)?|upgrad(?:e|es|ed|ing)|"
    r"select(?:s|ed|ing)?|pin(?:s|ned|ning)?|resolv(?:e|es|ed|ing)|"
    r"build(?:s|ing)?|built|run(?:s|ning)?|ran|deploy(?:s|ed|ing)?|"
    r"refresh(?:es|ed|ing)?|updat(?:e|es|ed|ing)|review(?:s|ed|ing)?|"
    r"check(?:s|ed|ing)?|us(?:e|es|ed|ing)|generat(?:e|es|ed|ing)|"
    r"quer(?:y|ies|ied|ying)|follow(?:s|ed|ing)?|send(?:s|ing)?|sent|"
    r"call(?:s|ed|ing)?|invok(?:e|es|ed|ing)|configur(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)
_PACKAGE_RULE = re.compile(
    r"\b(requires[- ]python|requires[- ]dist|declares?|requires?|minimum (?:supported )?"
    r"(?:python )?version|maximum (?:python )?version|version (?:constraint|range)|"
    r"dependency (?:constraint|range|requirement)|"
    r"required [A-Za-z0-9_.-]+ (?:constraint|range|requirement)|"
    r"classifiers?|wheel|source distribution|"
    r"sdist|extra|environment marker|yanked|vulnerab\w*|entry point|package rule)\b",
    re.IGNORECASE,
)
_CONCRETE_VALUE = re.compile(
    r"(?:[<>!=~^]=?|\bPython\s+\d+(?:\.\d+)?\b|\b\d+\.\d+(?:\.\d+)?\b|"
    r"\b(?:added|removed|yanked|true|false)\b)",
    re.IGNORECASE,
)
_CONCRETE_OUTCOME = re.compile(
    r"\b(rejects?|refuses?|fails?|cannot|can't|will not|won't|ineligible|"
    r"exclud(?:e|es|ed|ing)|"
    r"blocks?|prevents?|forces?|selects?|resolves?|changes?|requires?|recognizes?|"
    r"reports?|allows?|remov(?:e|es|ed|ing)|strip(?:s|ped|ping)?|"
    r"retain(?:s|ed|ing)?|emit(?:s|ted|ting)?|rais(?:e|es|ed|ing)|"
    r"return(?:s|ed|ing)?|send(?:s|ing)?|sent|withdraws?|unavailable|different)\b",
    re.IGNORECASE,
)
_RUNTIME_RULE = re.compile(
    r"\b(runtime behavior|request|response|redirect|header|warning|exception|method|"
    r"argument|parameter|option|setting|configuration|default|return value|timeout|retry)\b",
    re.IGNORECASE,
)
_RUNTIME_VALUE = re.compile(
    r"(?:\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b|"
    r"\b(?:non[- ]empty|empty|same host|different host|cross[- ]host|enabled|disabled)\b|"
    r"`[^`]+`|'[^']+'|\"[^\"]+\")",
    re.IGNORECASE,
)


def impact_case_kinds(change_types: Iterable[str]) -> tuple[ImpactCaseKind, ...]:
    """Map accepted materiality categories to strict applicability case shapes."""
    values = set(change_types)
    kinds: list[ImpactCaseKind] = []
    if values.intersection(
        {
            "dependency_contract",
            "python_compatibility",
            "platform_installability",
            "release_availability",
            "release_withdrawal",
            "security_advisory",
            "packaging_metadata",
        }
    ):
        kinds.append("metadata")
    if "runtime_behavior_unobservable" in values:
        kinds.append("runtime")
    return tuple(kinds) or ("metadata", "runtime")


def impact_case_errors(
    value: str,
    *,
    kinds: tuple[ImpactCaseKind, ...] = ("metadata",),
) -> tuple[str, ...]:
    """Return errors unless one evidence-authorized impact shape is complete."""
    if not _CAUSAL_LANGUAGE.search(value):
        return (
            "must connect the consumer action to the changed behavior with because, since, or due to",
        )

    candidates = [_impact_case_errors(value, kind) for kind in kinds]
    for errors in candidates:
        if not errors:
            return ()
    return min(candidates, key=len)


def _impact_case_errors(value: str, kind: ImpactCaseKind) -> tuple[str, ...]:
    errors: list[str] = []
    if not _CONSUMER_ACTION.search(value):
        errors.append("must state the consumer action or environment")
    if kind == "metadata":
        if not _PACKAGE_RULE.search(value) or not _CONCRETE_VALUE.search(value):
            errors.append("must state the exact changed package rule and a concrete value")
    elif not _RUNTIME_RULE.search(value) or not _RUNTIME_VALUE.search(value):
        errors.append("must state the exact runtime trigger or configuration and a concrete value")
    if not _CONCRETE_OUTCOME.search(value):
        errors.append(
            "must state the resulting installation, resolution, build, or runtime behavior"
        )
    return tuple(errors)


@dataclass(frozen=True)
class ValidationResult:
    """Collect deterministic, stable validation errors for one boundary."""

    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_deterministic_analysis(
    *,
    routing: RoutingEnvelope,
    disposition: Disposition,
    gate_results: Json,
    model_calls: list[ModelCallRecord],
    evidence: EvidenceBundle,
) -> ValidationResult:
    """Prove that a no-model terminal came from an auditable deterministic rule."""
    errors: list[str] = []
    if routing.analysis_eligibility == "model":
        errors.append("deterministic analysis cannot use the model route")
    if model_calls:
        errors.append("deterministic analysis cannot contain model calls")
    if routing.analysis_eligibility == "observe_only":
        if disposition != Disposition.PRERELEASE:
            errors.append("observe-only routing requires the prerelease disposition")
    elif routing.analysis_eligibility == "deterministic_non_substantive":
        triage = gate_results.get("deterministic_triage")
        if not isinstance(triage, dict):
            errors.append("deterministic analysis requires a triage result")
        else:
            expected = {
                "decision": "non_substantive",
                "rule_id": "no-scoped-release-change",
                "rule_version": "deterministic-triage-v1",
                "evidence_bundle_id": evidence.bundle_id,
            }
            for key, value in expected.items():
                if triage.get(key) != value:
                    errors.append(f"deterministic triage {key} does not match its proof input")
        if disposition != Disposition.NON_SUBSTANTIVE:
            errors.append("deterministic no-change routing requires non_substantive")
        if evidence.collection_status != "complete":
            errors.append("deterministic no-change routing requires complete evidence")
    elif routing.analysis_eligibility == "deterministic_impact":
        proof = gate_results.get("deterministic_impact")
        if not isinstance(proof, dict):
            errors.append("deterministic impact analysis requires a proof result")
        else:
            expected = {
                "rule_version": RULE_VERSION,
                "evidence_bundle_id": evidence.bundle_id,
                "scope": "pypi_release_selection_and_distribution_availability",
            }
            for key, value in expected.items():
                if proof.get(key) != value:
                    errors.append(f"deterministic impact {key} does not match its proof input")
            if proof.get("decision") not in {"impact_detected", "support_expanded"}:
                errors.append("deterministic impact has an unknown decision")
            impacts = proof.get("impacts")
            expansions = proof.get("support_expansions")
            if not isinstance(impacts, list) or not isinstance(expansions, list):
                errors.append("deterministic impact requires impact and support lists")
            elif not impacts and not expansions:
                errors.append("deterministic impact requires at least one resolved change")
            unknown_ids = _deterministic_evidence_ids(proof) - evidence.evidence_ids
            if unknown_ids:
                errors.append(
                    f"deterministic impact references unknown evidence: {sorted(unknown_ids)}"
                )
        if disposition != Disposition.PUBLISHABLE:
            errors.append("deterministic impact routing requires publishable")
        if evidence.collection_status != "complete":
            errors.append("deterministic impact routing requires complete evidence")
        materiality = gate_results.get("materiality")
        applicability = gate_results.get("applicability")
        customer_impact = gate_results.get("customer_impact")
        if not isinstance(materiality, dict) or materiality.get("decision") != "substantive":
            errors.append("deterministic impact requires substantive materiality output")
        if not isinstance(applicability, dict) or not applicability.get("consumer_scenarios"):
            errors.append("deterministic impact requires an applicability scenario")
        summary = (
            customer_impact.get("customer_summary") if isinstance(customer_impact, dict) else None
        )
        if not isinstance(summary, dict) or summary.get("decision") != "publishable_summary":
            errors.append("deterministic impact requires a publishable customer summary")
        if isinstance(materiality, dict) and isinstance(applicability, dict):
            try:
                materiality_result = MaterialityResult.from_dict(materiality)
                applicability_result = ApplicabilityResult.from_dict(applicability)
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"deterministic semantic output is malformed: {type(exc).__name__}")
            else:
                errors.extend(
                    f"deterministic materiality: {error}"
                    for error in validate_materiality(materiality_result, evidence).errors
                )
                errors.extend(
                    f"deterministic applicability: {error}"
                    for error in validate_applicability(
                        applicability_result,
                        materiality_result,
                        package=evidence.package,
                        candidate_version=str(evidence.candidate.get("version")),
                    ).errors
                )
                if isinstance(summary, dict):
                    try:
                        customer_result = CustomerImpactSummary.from_dict(summary)
                    except (KeyError, TypeError, ValueError) as exc:
                        errors.append(
                            f"deterministic customer impact is malformed: {type(exc).__name__}"
                        )
                    else:
                        errors.extend(
                            f"deterministic customer impact: {error}"
                            for error in validate_customer_impact_summary(
                                customer_result,
                                materiality_result,
                                applicability_result,
                                package=evidence.package,
                                candidate_version=str(evidence.candidate.get("version")),
                            ).errors
                        )
    else:
        errors.append("unknown deterministic analysis route")
    return ValidationResult(tuple(errors))


def _deterministic_evidence_ids(proof: Json) -> set[str]:
    result: set[str] = set()
    for key in ("impacts", "support_expansions"):
        items = proof.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("evidence_ids"), list):
                continue
            result.update(str(value) for value in item["evidence_ids"])
    return result


def validate_model_assisted_analysis(
    *,
    routing: RoutingEnvelope,
    disposition: Disposition,
    gate_results: Json,
    model_calls: list[ModelCallRecord],
) -> ValidationResult:
    """Prove that a reasoned terminal retained its model and semantic-stage lineage."""
    errors: list[str] = []
    if routing.analysis_eligibility != "model":
        errors.append("model-assisted analysis requires the model route")
    if not model_calls:
        errors.append("model-assisted analysis requires at least one model call")
    if "deterministic_triage" in gate_results:
        errors.append("model-assisted analysis cannot contain deterministic triage output")
    if "materiality" not in gate_results:
        errors.append("model-assisted analysis requires materiality output")
    if disposition == Disposition.PUBLISHABLE:
        customer_impact = gate_results.get("customer_impact")
        if not isinstance(customer_impact, dict) or customer_impact.get("valid") is not True:
            errors.append("publishable model-assisted analysis requires valid customer impact")
        summary = (
            customer_impact.get("customer_summary") if isinstance(customer_impact, dict) else None
        )
        if not isinstance(summary, dict) or summary.get("decision") != "publishable_summary":
            errors.append("publishable model-assisted analysis requires a publishable summary")
    return ValidationResult(tuple(errors))


def validate_publication_contract(
    *,
    analysis_method: AnalysisMethod,
    event_key: str,
    package: str,
    candidate_version: str,
    disposition: Disposition,
    evidence: EvidenceBundle,
    path_validation: ValidationResult,
) -> ValidationResult:
    """Apply identity and fail-closed checks shared by every terminal method."""
    errors: list[str] = []
    if evidence.event_key != event_key:
        errors.append("evidence event key does not match the terminal event")
    if evidence.package != package:
        errors.append("evidence package does not match the terminal package")
    if evidence.candidate.get("version") != candidate_version:
        errors.append("evidence candidate version does not match the terminal candidate")
    if not evidence.bundle_id:
        errors.append("terminal requires a versioned evidence bundle")
    if disposition == Disposition.PUBLISHABLE and not path_validation.valid:
        errors.append(f"publishable {analysis_method} analysis failed route validation")
    return ValidationResult(tuple(errors))


def validate_materiality(result: MaterialityResult, evidence: EvidenceBundle) -> ValidationResult:
    """Validate materiality claims, confidence, and evidence citations."""
    errors: list[str] = []
    if not 0 <= result.confidence <= 1:
        errors.append("confidence must be between 0 and 1")
    invalid_types = set(result.change_types) - CHANGE_TYPES
    if invalid_types:
        errors.append(f"unknown change types: {sorted(invalid_types)}")
    if result.decision == Decision.SUBSTANTIVE and not result.claims:
        errors.append("substantive decisions require at least one claim")
    if result.decision == Decision.INSUFFICIENT and not result.missing_evidence:
        errors.append("insufficient_evidence requires missing_evidence")
    for index, claim in enumerate(result.claims):
        prefix = f"claims[{index}]"
        if claim.support not in {"direct", "conditional"}:
            errors.append(f"{prefix} has invalid support")
        if not claim.evidence_ids:
            errors.append(f"{prefix} requires evidence_ids")
        unknown = set(claim.evidence_ids) - evidence.evidence_ids
        if unknown:
            errors.append(f"{prefix} references unknown evidence: {sorted(unknown)}")
        if claim.support == "conditional" and not claim.conditions:
            errors.append(f"{prefix} conditional support requires conditions")
        if _FORBIDDEN_SCOPE.search(claim.statement):
            errors.append(f"{prefix} asserts popularity or ranking facts")
        if _UNSUPPORTED_ABSOLUTE.search(claim.statement):
            errors.append(f"{prefix} contains unsupported absolute language")
    if "security_advisory" in result.change_types:
        cited = {evidence_id for claim in result.claims for evidence_id in claim.evidence_ids}
        if not cited.intersection(evidence.vulnerability_ids):
            errors.append("security_advisory requires structured vulnerability evidence")
    return ValidationResult(tuple(errors))


def validate_applicability(
    result: ApplicabilityResult,
    materiality: MaterialityResult,
    *,
    package: str,
    candidate_version: str,
) -> ValidationResult:
    """Validate consumer scenarios against accepted materiality and scope."""
    errors: list[str] = []
    case_kinds = impact_case_kinds(materiality.change_types)
    if not 0 <= result.confidence <= 1:
        errors.append("confidence must be between 0 and 1")
    if _UNSUPPORTED_ABSOLUTE.search(result.assessment):
        errors.append("assessment contains unsupported absolute language")
    if _FORBIDDEN_ORGANIZATION_CONTEXT.search(result.assessment):
        errors.append("assessment contains organization-specific context")
    accepted_evidence_ids = {
        evidence_id for claim in materiality.claims for evidence_id in claim.evidence_ids
    }
    if not result.consumer_scenarios:
        errors.append("applicability requires one structured consumer scenario")
    for index, scenario in enumerate(result.consumer_scenarios):
        prefix = f"consumer_scenarios[{index}]"
        if scenario.impact_kind not in case_kinds:
            errors.append(
                f"{prefix}.impact_kind {scenario.impact_kind} is not supported by accepted materiality"
            )
        if scenario.package != package:
            errors.append(f"{prefix}.package must equal {package}")
        if scenario.candidate_version != candidate_version:
            errors.append(f"{prefix}.candidate_version must equal {candidate_version}")
        for field, value in (
            ("consumer_trigger", scenario.consumer_trigger),
            ("changed_behavior", scenario.changed_behavior),
            ("observable_outcome", scenario.observable_outcome),
            ("verification", scenario.verification),
        ):
            if not value.strip():
                errors.append(f"{prefix}.{field} must not be empty")
        if _word_count(scenario.consumer_trigger) > 20:
            errors.append(f"{prefix}.consumer_trigger must contain at most 20 words")
        if len(scenario.consumer_trigger) > 160:
            errors.append(f"{prefix}.consumer_trigger must contain at most 160 characters")
        if not scenario.evidence_ids:
            errors.append(f"{prefix} requires accepted release evidence")
        unknown_evidence = set(scenario.evidence_ids) - accepted_evidence_ids
        if unknown_evidence:
            errors.append(
                f"{prefix} references unaccepted release evidence: {sorted(unknown_evidence)}"
            )
        if not scenario.conditions:
            errors.append(f"{prefix} requires impact conditions")
        for condition_index, condition in enumerate(scenario.conditions):
            if not condition.strip():
                errors.append(f"{prefix}.conditions[{condition_index}] must not be empty")
        if _UNSUPPORTED_ABSOLUTE.search(scenario.observable_outcome):
            errors.append(f"{prefix} contains unsupported absolute language")
        visible_scenario = " ".join(
            (
                scenario.consumer_trigger,
                scenario.changed_behavior,
                scenario.observable_outcome,
                scenario.verification,
                *scenario.conditions,
            )
        )
        if _FORBIDDEN_ORGANIZATION_CONTEXT.search(visible_scenario):
            errors.append(f"{prefix} contains organization-specific context")
    for index, limitation in enumerate(result.limitations):
        if _FORBIDDEN_ORGANIZATION_CONTEXT.search(limitation):
            errors.append(f"limitations[{index}] contains organization-specific context")
    return ValidationResult(tuple(errors))


def validate_customer_impact_summary(
    result: CustomerImpactSummary,
    materiality: MaterialityResult,
    applicability: ApplicabilityResult,
    *,
    package: str,
    candidate_version: str,
) -> ValidationResult:
    """Validate customer copy against accepted scenarios and guardrails."""
    errors: list[str] = []
    if result.impact_type not in IMPACT_TYPES:
        errors.append("unknown impact type")
    if result.decision == "insufficient_summary":
        display_values = (
            result.headline,
            result.affected_if,
            result.what_happens,
            result.not_affected_if,
            result.recommended_action,
            result.verification,
            result.reach_summary,
        )
        if any(value.strip() for value in display_values):
            errors.append("insufficient_summary must not publish display copy")
        if not result.limitations:
            errors.append("insufficient_summary must explain the missing support")
        if result.decision_card is not None:
            errors.append("insufficient_summary must not publish a decision card")
        return ValidationResult(tuple(errors))

    required = {
        "headline": result.headline,
        "affected_if": result.affected_if,
        "what_happens": result.what_happens,
        "not_affected_if": result.not_affected_if,
        "recommended_action": result.recommended_action,
        "verification": result.verification,
        "reach_summary": result.reach_summary,
    }
    errors.extend(f"{name} is required" for name, value in required.items() if not value.strip())
    if not _names_release(result.headline, package, candidate_version):
        errors.append("headline must name the package and exact candidate version")

    accepted_evidence_ids = {
        evidence_id for claim in materiality.claims for evidence_id in claim.evidence_ids
    }
    if not result.evidence_ids:
        errors.append("summary requires accepted release evidence")
    unknown_evidence = set(result.evidence_ids) - accepted_evidence_ids
    if unknown_evidence:
        errors.append(f"summary references unaccepted release evidence: {sorted(unknown_evidence)}")

    if not result.limitations:
        errors.append("summary requires explicit limitations")

    if len(applicability.consumer_scenarios) != 1:
        errors.append("summary requires exactly one accepted applicability scenario")
    else:
        scenario = applicability.consumer_scenarios[0]
        if result.affected_if != scenario.consumer_trigger:
            errors.append("affected_if must preserve the accepted consumer trigger")
        if result.what_happens != render_consumer_scenario_effect(scenario):
            errors.append("what_happens must preserve the accepted behavior and outcome")
        if result.verification != scenario.verification:
            errors.append("verification must preserve the accepted scenario check")
        if set(result.evidence_ids) != set(scenario.evidence_ids):
            errors.append("summary evidence must match the accepted scenario evidence")
        missing_limitations = set(applicability.limitations) - set(result.limitations)
        if missing_limitations:
            errors.append("summary must retain every applicability limitation")
        errors.extend(_validate_decision_card(result, scenario))

    if _word_count(result.headline) > 8:
        errors.append("headline must contain at most 8 words")
    if len(result.headline) > 70:
        errors.append("headline must contain at most 70 characters")
    if _word_count(result.recommended_action) > 20:
        errors.append("recommended_action must contain at most 20 words")
    if len(result.recommended_action) > 140:
        errors.append("recommended_action must contain at most 140 characters")

    visible = " ".join((*required.values(), *result.limitations))
    if _UNSUPPORTED_ABSOLUTE.search(visible):
        errors.append("summary contains unsupported absolute language")
    if _FORBIDDEN_ORGANIZATION_CONTEXT.search(visible):
        errors.append("summary contains organization-specific context")
    if re.search(
        r"\b(gate [234]|prompt|model call|trace|redpanda|internal ranking)\b",
        visible,
        re.I,
    ):
        errors.append("summary exposes internal pipeline language")
    if not re.search(r"\bmonitored\b", result.reach_summary, re.I):
        errors.append("reach_summary must identify monitored scope")
    return ValidationResult(tuple(dict.fromkeys(errors)))


def _names_release(value: str, package: str, candidate_version: str) -> bool:
    normalized = value.casefold()
    return package.casefold() in normalized and candidate_version.casefold() in normalized


def ground_customer_impact_summary(
    result: CustomerImpactSummary,
    applicability: ApplicabilityResult,
) -> CustomerImpactSummary:
    """Project the evidence-bearing display fields from the accepted typed scenario."""
    if result.decision != "publishable_summary" or len(applicability.consumer_scenarios) != 1:
        return result
    scenario = applicability.consumer_scenarios[0]
    limitations = tuple(dict.fromkeys((*applicability.limitations, *result.limitations)))
    return CustomerImpactSummary(
        decision=result.decision,
        impact_type=result.impact_type,
        headline=result.headline,
        affected_if=scenario.consumer_trigger,
        what_happens=render_consumer_scenario_effect(scenario),
        not_affected_if=result.not_affected_if,
        recommended_action=result.recommended_action,
        verification=scenario.verification,
        reach_summary=result.reach_summary,
        evidence_ids=tuple(dict.fromkeys(scenario.evidence_ids)),
        limitations=limitations,
        decision_card=DecisionCard(
            headline=result.headline,
            applies_when=scenario.consumer_trigger,
            action=result.recommended_action,
            source_scenario_id=_DECISION_CARD_SCENARIO_ID,
        ),
    )


def _validate_decision_card(
    result: CustomerImpactSummary,
    scenario: ConsumerScenario,
) -> list[str]:
    card = result.decision_card
    if card is None:
        return ["publishable summary requires a grounded decision card"]
    errors: list[str] = []
    if card.headline != result.headline:
        errors.append("decision card headline must preserve the validated headline")
    if card.applies_when != scenario.consumer_trigger:
        errors.append("decision card applies_when must preserve the accepted consumer trigger")
    if card.action != result.recommended_action:
        errors.append("decision card action must preserve the validated recommendation")
    if card.source_scenario_id != _DECISION_CARD_SCENARIO_ID:
        errors.append("decision card must reference scenario-1")
    return errors


def render_consumer_scenario_effect(scenario: ConsumerScenario) -> str:
    """Render accepted changed behavior and outcome as one concise effect."""
    changed_behavior = scenario.changed_behavior.strip()
    if changed_behavior and changed_behavior[-1] not in ".!?":
        changed_behavior += "."
    return f"{changed_behavior} {scenario.observable_outcome.strip()}".strip()


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[\w.-]+\b", value))
