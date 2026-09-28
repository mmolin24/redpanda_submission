"""Orchestrate enrichment, routing, reasoning, and terminal outcomes."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any, Protocol

from packaging.utils import parse_wheel_filename
from packaging.version import Version

from .deterministic_impact import compile_deterministic_impact
from .evidence import ExactReleaseNotFound
from .ids import deterministic_id, opaque_id, sha256_json
from .model_input import ModelInputTooLarge
from .models import (
    Decision,
    Disposition,
    EvidenceBundle,
    FailureRecord,
    FailureStage,
    Finding,
    Json,
    ModelCallRecord,
    ModelPurpose,
    Priority,
    ReasoningComplexity,
    ReasoningEffort,
    ReleaseEvent,
    RoutingEnvelope,
    ServiceTier,
    TraceContext,
    utc_now,
)
from .monitoring import MonitoredPackages
from .provider import (
    APPLICABILITY_SCHEMA,
    CUSTOMER_IMPACT_SCHEMA,
    MATERIALITY_SCHEMA,
    ProviderExhausted,
    ProviderIncomplete,
)
from .reasoning import (
    APPLICABILITY_INSTRUCTIONS,
    CUSTOMER_IMPACT_INSTRUCTIONS,
    MATERIALITY_INSTRUCTIONS,
    ApplicabilityEngine,
    CustomerImpactEngine,
    MaterialityEngine,
    ReasoningStageFailure,
    verify_customer_impact_inputs,
)
from .terminal import new_reasoning_failure
from .validation import (
    ANALYSIS_VALIDATION_POLICY_VERSION,
    validate_deterministic_analysis,
    validate_model_assisted_analysis,
    validate_publication_contract,
)


class Enricher(Protocol):
    """Build normalized evidence for one release event."""

    def enrich(self, event: ReleaseEvent, /) -> EvidenceBundle: ...


class ProcessingBackpressure(RuntimeError):
    """A non-terminal fault that must leave the source record unacknowledged."""

    def __init__(
        self,
        stage: FailureStage,
        error_class: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.error_class = error_class
        self.retryable = retryable


@dataclass(frozen=True)
class StaticEnricher:
    """Return one prebuilt evidence bundle for deterministic tests."""

    bundle: EvidenceBundle

    def enrich(self, event: ReleaseEvent) -> EvidenceBundle:
        if self.bundle.event_key != event.event_key:
            raise ValueError("static evidence does not match event")
        return self.bundle


class ReasoningPipeline:
    """Resolve every release into exactly one finding or failure."""

    def __init__(
        self,
        *,
        enricher: Enricher,
        materiality: MaterialityEngine,
        applicability: ApplicabilityEngine,
        customer_impact: CustomerImpactEngine | None = None,
        confidence_threshold: float = 0.65,
        environment_profiles: tuple[str, ...] = (
            "Python 3.10 on Linux",
            "Python 3.11 on Linux",
            "Python 3.12 on Linux",
            "Python 3.13 on Linux",
        ),
        monitored_packages: MonitoredPackages | None = None,
        analysis_policy_revision: str = "analysis-policy-v1",
    ) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", analysis_policy_revision) is None:
            raise ValueError("analysis policy revision must be a bounded stable identifier")
        self.enricher = enricher
        self.materiality = materiality
        self.applicability = applicability
        self.customer_impact = customer_impact or CustomerImpactEngine(applicability.provider)
        self.confidence_threshold = confidence_threshold
        self.environment_profiles = environment_profiles
        self.monitored_packages = monitored_packages
        self.analysis_policy_revision = analysis_policy_revision

    def process(
        self,
        event: ReleaseEvent,
        *,
        processing_attempt_id: str | None = None,
        trace_context: TraceContext | None = None,
    ) -> Finding | FailureRecord:
        attempt_id = processing_attempt_id or opaque_id()
        trace_context = (trace_context or TraceContext()).ensured()
        started_at = utc_now()
        stages: list[Json] = [
            dict(stage)
            for stage in event.observability.get("stage_summary", [])
            if isinstance(stage, dict)
        ]
        model_calls: list[ModelCallRecord] = []
        if self.monitored_packages and not self.monitored_packages.contains(
            event.package.normalized_name
        ):
            raise ProcessingBackpressure(
                "routing",
                "package_not_monitored",
                "release event package is not in the monitored-package config",
                retryable=False,
            )
        try:
            evidence = self._stage(stages, "enrichment", lambda: self.enricher.enrich(event))
            is_prerelease = Version(event.release.version).is_prerelease
            routing = self._stage(
                stages,
                "routing",
                lambda: route(is_prerelease=is_prerelease, evidence=evidence),
            )
            stages[-1]["detail"] = routing.analysis_eligibility
            if routing.analysis_eligibility == "observe_only":
                return self._finding(
                    event,
                    evidence,
                    routing,
                    Disposition.PRERELEASE,
                    {},
                    model_calls,
                    attempt_id,
                    trace_context,
                    stages,
                    started_at,
                )
            if routing.analysis_eligibility == "deterministic_non_substantive":
                return self._finding(
                    event,
                    evidence,
                    routing,
                    Disposition.NON_SUBSTANTIVE,
                    {
                        "deterministic_triage": {
                            "decision": "non_substantive",
                            "rule_id": "no-scoped-release-change",
                            "rule_version": "deterministic-triage-v1",
                            "evidence_bundle_id": evidence.bundle_id,
                            "reasons": list(routing.reasons),
                            "model_calls_avoided": 1,
                        }
                    },
                    model_calls,
                    attempt_id,
                    trace_context,
                    stages,
                    started_at,
                )
            if routing.analysis_eligibility == "deterministic_impact":
                deterministic_impact = compile_deterministic_impact(evidence)
                if deterministic_impact is None:
                    raise ReasoningStageFailure(
                        "routing",
                        "invalid_pipeline_state",
                        "deterministic impact route did not reproduce its proof",
                    )
                return self._finding(
                    event,
                    evidence,
                    routing,
                    Disposition.PUBLISHABLE,
                    deterministic_impact.gate_results,
                    model_calls,
                    attempt_id,
                    trace_context,
                    stages,
                    started_at,
                )
            materiality = self._stage(
                stages,
                "materiality",
                lambda: self.materiality.evaluate(evidence, routing),
            )
            model_calls.extend(materiality.calls)
            if materiality.disposition is not None:
                return self._finding(
                    event,
                    evidence,
                    routing,
                    materiality.disposition,
                    {"materiality": materiality.result.to_dict() if materiality.result else None},
                    model_calls,
                    attempt_id,
                    trace_context,
                    stages,
                    started_at,
                )
            if materiality.result is None or materiality.result.decision != Decision.SUBSTANTIVE:
                raise ReasoningStageFailure(
                    "materiality",
                    "invalid_pipeline_state",
                    "materiality assessment did not reach a terminal or substantive state",
                )

            applicability = self._stage(
                stages,
                "applicability",
                lambda: self.applicability.evaluate(
                    materiality.result,
                    routing,
                    package=event.package.normalized_name,
                    candidate_version=event.release.version,
                    environment_profiles=self.environment_profiles,
                ),
            )
            model_calls.extend(applicability.calls)
            versions = self._versions(event, evidence, routing)
            customer_impact_inputs = verify_customer_impact_inputs(
                materiality=materiality.result,
                applicability=applicability.result,
                applicability_validation=applicability.validation,
                evidence=evidence,
                confidence_threshold=self.confidence_threshold,
                versions=versions,
            )
            if customer_impact_inputs.valid:
                customer_impact = self._stage(
                    stages,
                    "customer_impact",
                    lambda: self.customer_impact.evaluate(
                        materiality.result,
                        applicability.result,
                        evidence,
                        package=event.package.normalized_name,
                        baseline_version=evidence.baseline.get("version"),
                        candidate_version=event.release.version,
                        environment_profiles=self.environment_profiles,
                    ),
                )
                model_calls.extend(customer_impact.calls)
                customer_impact_validation = customer_impact.validation
            else:
                customer_impact = None
                customer_impact_validation = customer_impact_inputs
            if (
                customer_impact_validation.valid
                and customer_impact is not None
                and customer_impact.result.decision == "publishable_summary"
            ):
                disposition = Disposition.PUBLISHABLE
            elif (
                customer_impact is not None
                and customer_impact.result.decision == "insufficient_summary"
            ):
                disposition = Disposition.INSUFFICIENT
            elif any("confidence below" in error for error in customer_impact_validation.errors):
                disposition = Disposition.LOW_CONFIDENCE
            else:
                disposition = Disposition.VALIDATION_FAILURE
            return self._finding(
                event,
                evidence,
                routing,
                disposition,
                {
                    "materiality": materiality.result.to_dict(),
                    "applicability": applicability.result.to_dict(),
                    "customer_impact": {
                        "valid": customer_impact_validation.valid,
                        "errors": list(customer_impact_validation.errors),
                        "customer_summary": customer_impact.result.to_dict()
                        if customer_impact is not None
                        else None,
                    },
                    "versions": versions,
                },
                model_calls,
                attempt_id,
                trace_context,
                stages,
                started_at,
            )
        except ReasoningStageFailure as exc:
            model_calls.extend(call for call in exc.calls if call not in model_calls)
            return self._failure(
                event,
                attempt_id,
                trace_context,
                exc.stage,
                exc.error_class,
                str(exc),
                False,
                stages,
                model_calls,
            )
        except ProviderExhausted as exc:
            model_calls.append(exc.call)
            raise ProcessingBackpressure(
                self._model_stage(exc.call.purpose),
                "openai_attempts_exhausted",
                "model provider attempts exhausted",
                retryable=exc.retryable,
            ) from exc
        except ProviderIncomplete as exc:
            model_calls.append(exc.call)
            raise ProcessingBackpressure(
                self._model_stage(exc.call.purpose),
                "openai_incomplete_response",
                "model provider returned an incomplete response",
                retryable=True,
            ) from exc
        except ModelInputTooLarge as exc:
            stage = stages[-1]["stage"] if stages else "materiality"
            raise ProcessingBackpressure(
                stage,
                "model_input_budget_exceeded",
                "compiled model input exceeded the provider budget",
                retryable=False,
            ) from exc
        except ExactReleaseNotFound:
            return self._failure(
                event,
                attempt_id,
                trace_context,
                "enrichment",
                "exact_release_not_found",
                "exact package release metadata is no longer available",
                False,
                stages,
                model_calls,
            )
        except Exception as exc:
            stage = stages[-1]["stage"] if stages else "enrichment"
            raise ProcessingBackpressure(
                stage
                if stage
                in {
                    "enrichment",
                    "routing",
                    "materiality",
                    "applicability",
                    "customer_impact",
                }
                else "routing",
                type(exc).__name__.lower(),
                "worker stage failed; inspect correlated local logs",
                retryable=_retryable_processing_error(exc),
            ) from exc

    @staticmethod
    def _stage(stages: list[Json], name: str, callback):
        started_wall = utc_now()
        started = monotonic()
        try:
            result = callback()
        except Exception:
            stages.append(
                {
                    "stage": name,
                    "outcome": "failed",
                    "started_at": started_wall,
                    "completed_at": utc_now(),
                    "detail": f"duration_ms={round((monotonic() - started) * 1000, 3)}",
                }
            )
            raise
        stages.append(
            {
                "stage": name,
                "outcome": "completed",
                "started_at": started_wall,
                "completed_at": utc_now(),
                "detail": f"duration_ms={round((monotonic() - started) * 1000, 3)}",
            }
        )
        return result

    @staticmethod
    def _model_stage(purpose: ModelPurpose) -> FailureStage:
        if purpose.startswith("materiality"):
            return "materiality"
        if purpose.startswith("customer_impact"):
            return "customer_impact"
        return "applicability"

    def _versions(
        self,
        event: ReleaseEvent,
        evidence: EvidenceBundle,
        routing,
    ) -> Json:
        pieces = {
            "analysis_policy_revision": self.analysis_policy_revision,
            "materiality_prompt_hash": sha256_json(MATERIALITY_INSTRUCTIONS),
            "applicability_prompt_hash": sha256_json(APPLICABILITY_INSTRUCTIONS),
            "materiality_schema_hash": sha256_json(MATERIALITY_SCHEMA),
            "applicability_schema_hash": sha256_json(APPLICABILITY_SCHEMA),
            "customer_impact_prompt_hash": sha256_json(CUSTOMER_IMPACT_INSTRUCTIONS),
            "customer_impact_schema_hash": sha256_json(CUSTOMER_IMPACT_SCHEMA),
            "evidence_bundle_id": evidence.bundle_id,
            "routing_policy_version": routing.policy_version,
            "customer_impact_policy_version": "customer-impact-summary-v5",
            "analysis_validation_policy_version": ANALYSIS_VALIDATION_POLICY_VERSION,
        }
        pieces["analysis_version"] = sha256_json(pieces)
        return pieces

    def _finding(
        self,
        event: ReleaseEvent,
        evidence: EvidenceBundle,
        routing,
        disposition: Disposition,
        gate_results: Json,
        model_calls: list[ModelCallRecord],
        attempt_id: str,
        trace_context: TraceContext,
        stages: list[Json],
        started_at: str,
    ) -> Finding:
        versions = gate_results.get("versions") or self._versions(event, evidence, routing)
        analysis_version = versions["analysis_version"]
        finding_id = deterministic_id(event.event_key, analysis_version)
        analysis_method = (
            "model_assisted" if routing.analysis_eligibility == "model" else "deterministic"
        )
        path_validation = (
            validate_model_assisted_analysis(
                routing=routing,
                disposition=disposition,
                gate_results=gate_results,
                model_calls=model_calls,
            )
            if analysis_method == "model_assisted"
            else validate_deterministic_analysis(
                routing=routing,
                disposition=disposition,
                gate_results=gate_results,
                model_calls=model_calls,
                evidence=evidence,
            )
        )
        publication_validation = validate_publication_contract(
            analysis_method=analysis_method,
            event_key=event.event_key,
            package=event.package.normalized_name,
            candidate_version=event.release.version,
            disposition=disposition,
            evidence=evidence,
            path_validation=path_validation,
        )
        gate_results = {
            **gate_results,
            "analysis_validation": {
                "policy_version": ANALYSIS_VALIDATION_POLICY_VERSION,
                "analysis_method": analysis_method,
                "path": {
                    "valid": path_validation.valid,
                    "errors": list(path_validation.errors),
                },
                "publication": {
                    "valid": publication_validation.valid,
                    "errors": list(publication_validation.errors),
                },
            },
        }
        if not path_validation.valid or not publication_validation.valid:
            disposition = Disposition.VALIDATION_FAILURE
        completed_at = utc_now()
        observability = trace_context.to_observability(attempt_id, stages)
        metadata = {
            "analysis_version": analysis_version,
            "processing_attempt_id": attempt_id,
            "model_calls": [asdict(call) for call in model_calls],
            "started_at": started_at,
            "completed_at": completed_at,
            "observability": observability,
            "versions": versions,
        }
        return Finding(
            finding_id=finding_id,
            event_key=event.event_key,
            source_event=event.to_dict(),
            disposition=disposition,
            analysis_method=analysis_method,
            package=event.package.normalized_name,
            baseline_version=evidence.baseline.get("version"),
            candidate_version=event.release.version,
            gate_results=gate_results,
            evidence_bundle=evidence.to_dict(),
            routing=asdict(routing),
            analysis_metadata=metadata,
            publishable=disposition == Disposition.PUBLISHABLE,
            observability=observability,
        )

    @staticmethod
    def _failure(
        event: ReleaseEvent,
        attempt_id: str,
        trace_context: TraceContext,
        stage: FailureStage,
        error_class: str,
        message: str,
        retryable: bool,
        stages: list[Json],
        model_calls: list[ModelCallRecord] | None = None,
        *,
        attempt_count: int = 1,
    ) -> FailureRecord:
        return new_reasoning_failure(
            event_key=event.event_key,
            stage=stage,
            error_class=error_class,
            message=message,
            retryable=retryable,
            attempt_count=attempt_count,
            payload={
                "processing_attempt_id": attempt_id,
                "source_event": event.to_dict(),
                "model_calls": [asdict(call) for call in (model_calls or [])],
                "stage_summary": stages,
            },
            observability=trace_context.to_observability(attempt_id, stages),
        )


def _retryable_processing_error(exc: Exception) -> bool:
    status = getattr(exc, "code", None)
    if status in {408, 409, 429} or (isinstance(status, int) and status >= 500):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "APITimeoutError",
        "APIConnectionError",
        "URLError",
    }


def route(
    *,
    is_prerelease: bool,
    evidence: EvidenceBundle,
) -> RoutingEnvelope:
    """Choose the cheapest conclusive analysis path for compiled evidence."""
    complexity = _complexity(evidence)
    if is_prerelease:
        return RoutingEnvelope(
            processing_priority=Priority.LOW,
            analysis_eligibility="observe_only",
            reasoning_complexity=complexity,
            reasons=("prerelease_observation",),
            service_tier=None,
            model=None,
            reasoning_effort=None,
        )
    deterministic_reasons = _deterministic_non_substantive_reasons(evidence)
    if deterministic_reasons:
        return RoutingEnvelope(
            processing_priority=Priority.SKIP,
            analysis_eligibility="deterministic_non_substantive",
            reasoning_complexity=complexity,
            reasons=deterministic_reasons,
            service_tier=None,
            model=None,
            reasoning_effort=None,
        )
    deterministic_impact = compile_deterministic_impact(evidence)
    if deterministic_impact is not None:
        return RoutingEnvelope(
            processing_priority=Priority.MEDIUM,
            analysis_eligibility="deterministic_impact",
            reasoning_complexity=complexity,
            reasons=deterministic_impact.route_reasons,
            service_tier=None,
            model=None,
            reasoning_effort=None,
        )
    priority = Priority.HIGH if complexity == "complex" else Priority.MEDIUM
    tier = ServiceTier.DEFAULT if complexity == "complex" else ServiceTier.FLEX
    return RoutingEnvelope(
        processing_priority=priority,
        analysis_eligibility="model",
        reasoning_complexity=complexity,
        reasons=("explicitly_monitored_package", f"evidence_complexity:{complexity}"),
        service_tier=tier,
        model="gpt-5.6-sol",
        reasoning_effort=ReasoningEffort.LOW,
    )


def _deterministic_non_substantive_reasons(evidence: EvidenceBundle) -> tuple[str, ...]:
    """Return a terminal route only when complete evidence proves no scoped change."""
    if evidence.collection_status != "complete":
        return ()

    scoped_computed = {
        key: value
        for key, value in evidence.computed.items()
        if key not in {"files_diff", "missing"} and value not in (None, "", [], {})
    }
    if scoped_computed or evidence.computed.get("missing"):
        return ()
    if _metadata_without_version(evidence.baseline) != _metadata_without_version(
        evidence.candidate
    ):
        return ()
    if _has_semantic_context(evidence.context):
        return ()

    baseline_surface = _distribution_surface(evidence.baseline.get("files"))
    candidate_surface = _distribution_surface(evidence.candidate.get("files"))
    if baseline_surface is None or candidate_surface is None:
        return ()
    if baseline_surface != candidate_surface:
        return ()

    return (
        "explicitly_monitored_package",
        "complete_evidence",
        "no_scoped_release_change",
    )


def _metadata_without_version(release: dict[str, Any]) -> dict[str, Any]:
    metadata = release.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    return {key: value for key, value in metadata.items() if key not in {"name", "version"}}


def _has_semantic_context(context: dict[str, Any]) -> bool:
    ignored = {"repository_mapping_confidence", "fixture", "artifact_comparison"}
    for key, value in context.items():
        if key in ignored or value in (None, "", [], {}):
            continue
        if key == "artifact_manifest_diff" and isinstance(value, dict):
            if not any(value.get(name) for name in ("added", "removed", "changed")):
                continue
        return True
    return False


def _distribution_surface(files: Any) -> tuple[tuple[str, ...], ...] | None:
    if not isinstance(files, list):
        return None
    signatures: list[tuple[str, ...]] = []
    for item in files:
        if not isinstance(item, dict):
            return None
        filename = item.get("filename")
        package_type = item.get("packagetype")
        if not isinstance(filename, str) or not isinstance(package_type, str):
            return None
        signature = _distribution_signature(filename, package_type)
        if signature is None:
            return None
        signatures.append(signature)
    return tuple(sorted(signatures))


def _distribution_signature(filename: str, package_type: str) -> tuple[str, ...] | None:
    if package_type == "bdist_wheel" and filename.endswith(".whl"):
        try:
            _, _, build, tags = parse_wheel_filename(filename)
        except ValueError:
            return None
        build_value = ".".join(str(part) for part in build) if build else ""
        return ("wheel", build_value, *(str(tag) for tag in sorted(tags, key=str)))
    if package_type == "sdist":
        archive_format = next(
            (
                suffix
                for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar", ".zip")
                if filename.endswith(suffix)
            ),
            None,
        )
        return ("sdist", archive_format) if archive_format else None
    return None


def _complexity(evidence: EvidenceBundle) -> ReasoningComplexity:
    changed = sum(
        bool(evidence.computed.get(name))
        for name in (
            "requires_python_diff",
            "requires_dist_diff",
            "files_diff",
            "yank_diff",
            "vulnerability_diff",
        )
    )
    has_markers = any(";" in str(f.value) for f in evidence.facts)
    if evidence.collection_status != "complete" or changed >= 4:
        return "complex"
    if changed >= 2 or has_markers:
        return "moderate"
    return "simple"
