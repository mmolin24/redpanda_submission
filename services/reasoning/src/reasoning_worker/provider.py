"""Provide bounded OpenAI and offline model execution adapters."""

from __future__ import annotations

import json
import os
import random
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Protocol

from .ids import opaque_id, sha256_json
from .model_input import (
    PROVIDER_USER_JSON_MAX_BYTES,
    REASONING_MODEL_INPUT_MAX_BYTES,
    CompiledModelInput,
    compile_model_input,
)
from .models import (
    Json,
    ModelCallRecord,
    ModelPurpose,
    PhysicalAttempt,
    ReasoningEffort,
    ServiceTier,
    TokenUsage,
    utc_now,
)
from .sanitization import sanitize
from .telemetry import WorkerTelemetry

MATERIALITY_SCHEMA: Json = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision",
        "change_types",
        "claims",
        "missing_evidence",
        "confidence",
    ],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["substantive", "non_substantive", "insufficient_evidence"],
        },
        "change_types": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "string",
                "enum": [
                    "dependency_contract",
                    "python_compatibility",
                    "platform_installability",
                    "release_availability",
                    "release_withdrawal",
                    "security_advisory",
                    "packaging_metadata",
                    "runtime_behavior_unobservable",
                    "unknown",
                ],
            },
        },
        "claims": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["statement", "evidence_ids", "support", "conditions"],
                "properties": {
                    "statement": {"type": "string"},
                    "evidence_ids": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {"type": "string"},
                    },
                    "support": {"type": "string", "enum": ["direct", "conditional"]},
                    "conditions": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "missing_evidence": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string"},
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_CONSUMER_SCENARIO_SCHEMA: Json = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "impact_kind",
        "package",
        "candidate_version",
        "consumer_trigger",
        "changed_behavior",
        "observable_outcome",
        "verification",
        "evidence_ids",
        "conditions",
    ],
    "properties": {
        "impact_kind": {"type": "string", "enum": ["metadata", "runtime"]},
        "package": {"type": "string", "minLength": 1, "maxLength": 256},
        "candidate_version": {"type": "string", "minLength": 1, "maxLength": 128},
        "consumer_trigger": {"type": "string", "minLength": 1, "maxLength": 512},
        "changed_behavior": {"type": "string", "minLength": 1, "maxLength": 1024},
        "observable_outcome": {"type": "string", "minLength": 1, "maxLength": 512},
        "verification": {"type": "string", "minLength": 1, "maxLength": 512},
        "evidence_ids": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "enum": []},
        },
        "conditions": {"type": "array", "maxItems": 1, "items": {"type": "string"}},
    },
}

APPLICABILITY_SCHEMA: Json = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "assessment",
        "consumer_scenarios",
        "confidence",
        "limitations",
    ],
    "properties": {
        "assessment": {"type": "string"},
        "consumer_scenarios": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": _CONSUMER_SCENARIO_SCHEMA,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "limitations": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
    },
}


def applicability_schema_with_evidence_ids(evidence_ids: set[str]) -> Json:
    """Bind applicability citations to evidence already accepted by materiality."""
    schema = deepcopy(APPLICABILITY_SCHEMA)
    scenario = schema["properties"]["consumer_scenarios"]["items"]
    scenario["properties"]["evidence_ids"]["items"]["enum"] = sorted(evidence_ids)
    return schema


CUSTOMER_IMPACT_SCHEMA: Json = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision",
        "impact_type",
        "headline",
        "affected_if",
        "what_happens",
        "not_affected_if",
        "recommended_action",
        "verification",
        "reach_summary",
        "evidence_ids",
        "limitations",
    ],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["publishable_summary", "insufficient_summary"],
        },
        "impact_type": {
            "type": "string",
            "enum": [
                "install_block",
                "dependency_conflict",
                "dependency_upgrade",
                "runtime_compatibility",
                "platform_installability",
                "release_availability",
                "release_withdrawal",
                "security_signal",
                "other",
            ],
        },
        "headline": {"type": "string"},
        "affected_if": {"type": "string"},
        "what_happens": {"type": "string"},
        "not_affected_if": {"type": "string"},
        "recommended_action": {"type": "string"},
        "verification": {"type": "string"},
        "reach_summary": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass(frozen=True)
class ModelRequest:
    """Define one bounded, schema-constrained logical model request."""

    purpose: ModelPurpose
    instructions: str
    model_input: Json
    output_schema: Json
    output_schema_name: str
    instruction_suffix: str | None = None
    cache_namespace: str | None = None
    model: str = "gpt-5.6-sol"
    service_tier: ServiceTier = ServiceTier.DEFAULT
    reasoning_effort: ReasoningEffort = ReasoningEffort.LOW
    max_output_tokens: int = 1200
    max_input_bytes: int = REASONING_MODEL_INPUT_MAX_BYTES

    def compiled_input(self) -> CompiledModelInput:
        if self.max_input_bytes <= 0:
            raise ValueError("max_input_bytes must be positive")
        return compile_model_input(
            self.model_input,
            max_bytes=min(
                self.max_input_bytes,
                PROVIDER_USER_JSON_MAX_BYTES,
            ),
        )

    def persisted_payload(self, compiled_input: CompiledModelInput) -> Json:
        return {
            "model": self.model,
            "service_tier": self.service_tier.value,
            "reasoning": {"effort": self.reasoning_effort.value},
            "store": False,
            "max_output_tokens": self.max_output_tokens,
            "instructions": self.instructions,
            "instruction_suffix": self.instruction_suffix,
            "cache_namespace": self.cache_namespace,
            "input": compiled_input.value,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": self.output_schema_name,
                    "strict": True,
                    "schema": self.output_schema,
                }
            },
        }


@dataclass(frozen=True)
class ProviderResult:
    """Return parsed output or refusal metadata with its call record."""

    parsed: Json | None
    refusal: str | None
    call: ModelCallRecord


class ModelProvider(Protocol):
    """Execute one logical structured-output model request."""

    def complete(self, request: ModelRequest, /) -> ProviderResult: ...


class ProviderExhausted(RuntimeError):
    """Report a model request that exhausted its bounded attempts."""

    def __init__(self, message: str, call: ModelCallRecord, *, retryable: bool) -> None:
        super().__init__(message)
        self.call = call
        self.attempts = call.attempts
        self.retryable = retryable


class ProviderIncomplete(RuntimeError):
    """Report a provider response that did not complete generation."""

    def __init__(self, message: str, call: ModelCallRecord) -> None:
        super().__init__(message)
        self.call = call


_CAPTURE_POLICY_FACTORY_TOKEN = object()


@dataclass(frozen=True, init=False)
class ModelPayloadCapturePolicy:
    """Hold the fail-closed local model-payload capture decision."""

    enabled: bool

    def __init__(self, enabled: bool, *, _factory_token: object) -> None:
        if _factory_token is not _CAPTURE_POLICY_FACTORY_TOKEN:
            raise TypeError("use ModelPayloadCapturePolicy.from_environment()")
        object.__setattr__(self, "enabled", enabled)

    @classmethod
    def from_environment(cls) -> ModelPayloadCapturePolicy:
        return cls(
            capture_model_payloads_from_environment(),
            _factory_token=_CAPTURE_POLICY_FACTORY_TOKEN,
        )


def capture_model_payloads_from_environment() -> bool:
    """Enable payload capture only when both local-only controls allow it."""
    raw_flag = os.getenv("OBS_CAPTURE_MODEL_PAYLOADS")
    if raw_flag is None:
        requested = False
    elif raw_flag == "true":
        requested = True
    elif raw_flag == "false":
        requested = False
    else:
        raise ValueError("OBS_CAPTURE_MODEL_PAYLOADS must be true or false")
    return os.getenv("DEPLOYMENT_ENV") == "local" and requested


def _resolve_capture_policy(
    policy: ModelPayloadCapturePolicy | None,
) -> ModelPayloadCapturePolicy:
    resolved = policy or ModelPayloadCapturePolicy.from_environment()
    if not isinstance(resolved, ModelPayloadCapturePolicy):
        raise TypeError("capture_policy must come from the environment policy factory")
    return resolved


class _CapturePolicyBound:
    _capture_policy: ModelPayloadCapturePolicy

    def _set_capture_policy(
        self,
        policy: ModelPayloadCapturePolicy | None,
    ) -> None:
        self._capture_policy = _resolve_capture_policy(policy)

    @property
    def capture_payloads(self) -> bool:
        """Read-only compatibility view of the immutable capture policy."""
        return self._capture_policy.enabled


def _api_request_body(
    request: ModelRequest,
    compiled_input: CompiledModelInput,
    *,
    prompt_cache_enabled: bool,
) -> Json:
    instructions = request.instructions
    if request.instruction_suffix:
        instructions = f"{instructions}\n\n{request.instruction_suffix}"
    body: Json = {
        "model": request.model,
        "input": [
            {"role": "developer", "content": instructions},
            {"role": "user", "content": compiled_input.canonical_json},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": request.output_schema_name,
                "strict": True,
                "schema": request.output_schema,
            }
        },
        "reasoning": {"effort": request.reasoning_effort.value},
        "service_tier": request.service_tier.value,
        "max_output_tokens": request.max_output_tokens,
        "store": False,
    }
    if prompt_cache_enabled and request.cache_namespace:
        developer_content: list[Json] = [
            {
                "type": "input_text",
                "text": request.instructions,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            }
        ]
        if request.instruction_suffix:
            developer_content.append({"type": "input_text", "text": request.instruction_suffix})
        body["input"][0]["content"] = developer_content
        body["prompt_cache_key"] = _prompt_cache_key(request)
        body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
    elif not prompt_cache_enabled:
        body["prompt_cache_options"] = {"mode": "explicit"}
    return body


class OpenAIResponsesProvider(_CapturePolicyBound):
    """Responses API adapter with application-owned retries and sanitized capture."""

    def __init__(
        self,
        client: Any,
        *,
        telemetry: WorkerTelemetry | None = None,
        prompt_cache_enabled: bool = True,
        capture_policy: ModelPayloadCapturePolicy | None = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.client = client
        self.telemetry = telemetry or WorkerTelemetry()
        self.prompt_cache_enabled = prompt_cache_enabled
        self._set_capture_policy(capture_policy)
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.random_value = random_value

    def complete(self, request: ModelRequest) -> ProviderResult:
        model_call_id = opaque_id()
        compiled_input = request.compiled_input()
        api_body = _api_request_body(
            request,
            compiled_input,
            prompt_cache_enabled=self.prompt_cache_enabled,
        )
        request_sha256 = sha256_json(api_body)
        persisted_request = api_body if self._capture_policy.enabled else None
        attempts: list[PhysicalAttempt] = []
        last_error: Exception | None = None
        call_started = time.monotonic()
        with self.telemetry.model_call_span(request, model_call_id) as model_span:
            span_id = model_span.span_id()
            for number in range(1, self.max_attempts + 1):
                attempt_id = opaque_id()
                started_wall = utc_now()
                started = time.monotonic()
                try:
                    with self.telemetry.http_attempt_span(number, attempt_id):
                        scoped = self.client.with_options(
                            max_retries=0,
                            default_headers={"X-Client-Request-Id": attempt_id},
                        )
                        response = scoped.responses.create(**api_body)
                    latency = (time.monotonic() - started) * 1000
                    parsed, refusal = _visible_output(response)
                    safe_parsed = sanitize(parsed)
                    usage = _usage(response)
                    visible_response = sanitize(
                        {
                            "id": getattr(response, "id", None),
                            "status": getattr(response, "status", None),
                            "model": getattr(response, "model", None),
                            "service_tier": getattr(response, "service_tier", None),
                            "parsed": safe_parsed,
                            "refusal": refusal,
                            "incomplete_details": _public_dump(
                                getattr(response, "incomplete_details", None)
                            ),
                            "usage": asdict(usage),
                        }
                    )
                    request_id, processing_ms = _correlation(response)
                    attempt = PhysicalAttempt(
                        attempt_id=attempt_id,
                        client_request_id=attempt_id,
                        attempt_number=number,
                        outcome="refused"
                        if refusal
                        else str(getattr(response, "status", "completed")),
                        started_at=started_wall,
                        completed_at=utc_now(),
                        latency_ms=latency,
                        response_id=getattr(response, "id", None),
                        openai_request_id=request_id,
                        openai_processing_ms=processing_ms,
                    )
                    attempts.append(attempt)
                    status = getattr(response, "status", "completed")
                    outcome = "refused" if refusal else "completed"
                    call = ModelCallRecord(
                        model_call_id=model_call_id,
                        purpose=request.purpose,
                        requested_model=request.model,
                        returned_model=getattr(response, "model", None),
                        requested_service_tier=request.service_tier.value,
                        returned_service_tier=getattr(response, "service_tier", None),
                        reasoning_effort=request.reasoning_effort.value,
                        request_payload=persisted_request,
                        response_payload=(
                            visible_response if self._capture_policy.enabled else None
                        ),
                        request_sha256=request_sha256,
                        response_sha256=sha256_json(visible_response),
                        usage=usage,
                        attempts=tuple(attempts),
                        outcome=outcome if status == "completed" else "incomplete",
                        estimated_cost_usd=_estimate_cost(
                            usage,
                            model=request.model,
                            service_tier=request.service_tier.value,
                        ),
                        span_id=span_id,
                    )
                    if status != "completed":
                        model_span.set_error("incomplete")
                        self.telemetry.record_model_call(call, time.monotonic() - call_started)
                        raise ProviderIncomplete(f"response status was {status}", call)
                    model_span.set_result(call)
                    self.telemetry.record_model_call(call, time.monotonic() - call_started)
                    return ProviderResult(safe_parsed, refusal, call)
                except ProviderIncomplete:
                    raise
                except Exception as exc:
                    latency = (time.monotonic() - started) * 1000
                    last_error = exc
                    attempts.append(
                        PhysicalAttempt(
                            attempt_id=attempt_id,
                            client_request_id=attempt_id,
                            attempt_number=number,
                            outcome="error",
                            started_at=started_wall,
                            completed_at=utc_now(),
                            latency_ms=latency,
                            error_class=_error_class(exc),
                        )
                    )
                    if not _retryable(exc) or number == self.max_attempts:
                        model_span.set_error(_error_class(exc))
                        call = _failed_call(
                            model_call_id,
                            request,
                            persisted_request,
                            request_sha256,
                            tuple(attempts),
                            span_id,
                        )
                        self.telemetry.record_model_call(call, time.monotonic() - call_started)
                        raise ProviderExhausted(
                            f"OpenAI request exhausted after {number} attempt(s): {_error_class(exc)}",
                            call,
                            retryable=_retryable(exc),
                        ) from exc
                    self.sleep((0.25 * (2 ** (number - 1))) + (0.1 * self.random_value()))
        raise ProviderExhausted(
            "OpenAI request failed",
            _failed_call(
                model_call_id,
                request,
                persisted_request,
                request_sha256,
                tuple(attempts),
                span_id,
            ),
            retryable=last_error is not None and _retryable(last_error),
        ) from last_error


@dataclass(frozen=True)
class FakeOutcome:
    """Script one offline provider result, refusal, or error."""

    parsed: Json | None = None
    refusal: str | None = None
    error: BaseException | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)


class FakeModelProvider(_CapturePolicyBound):
    """Deterministic provider; each logical call consumes exactly one scripted outcome."""

    def __init__(
        self,
        outcomes: list[FakeOutcome],
        *,
        telemetry: WorkerTelemetry | None = None,
        capture_policy: ModelPayloadCapturePolicy | None = None,
    ) -> None:
        self.outcomes: deque[FakeOutcome] = deque(outcomes)
        self.requests: list[ModelRequest] = []
        self.telemetry = telemetry
        self._set_capture_policy(capture_policy)

    def complete(self, request: ModelRequest) -> ProviderResult:
        compiled_input = request.compiled_input()
        compiled_request = replace(
            request,
            model_input=compiled_input.value,
        )
        return self._complete_compiled(compiled_request, compiled_input)

    def _complete_compiled(
        self,
        request: ModelRequest,
        compiled_input: CompiledModelInput,
    ) -> ProviderResult:
        """Complete a request whose structured input has already been compiled."""
        started = time.monotonic()
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("fake provider received an unscripted call")
        outcome = self.outcomes.popleft()
        if outcome.error:
            raise outcome.error
        attempt_id = opaque_id()
        request_body = request.persisted_payload(compiled_input)
        safe_parsed = sanitize(outcome.parsed)
        visible_response = sanitize(
            {
                "parsed": safe_parsed,
                "refusal": outcome.refusal,
            }
        )
        call = ModelCallRecord(
            model_call_id=opaque_id(),
            purpose=request.purpose,
            requested_model=request.model,
            returned_model=request.model,
            requested_service_tier=request.service_tier.value,
            returned_service_tier=request.service_tier.value,
            reasoning_effort=request.reasoning_effort.value,
            request_payload=(request_body if self._capture_policy.enabled else None),
            response_payload=(visible_response if self._capture_policy.enabled else None),
            request_sha256=sha256_json(request_body),
            response_sha256=sha256_json(visible_response),
            usage=outcome.usage,
            attempts=(
                PhysicalAttempt(
                    attempt_id=attempt_id,
                    client_request_id=attempt_id,
                    attempt_number=1,
                    outcome="refused" if outcome.refusal else "completed",
                    started_at=utc_now(),
                    completed_at=utc_now(),
                    latency_ms=0,
                    response_id="resp_fake",
                    openai_request_id="req_fake",
                ),
            ),
            outcome="refused" if outcome.refusal else "completed",
            estimated_cost_usd=_estimate_cost(
                outcome.usage,
                model=request.model,
                service_tier=request.service_tier.value,
            ),
        )
        if self.telemetry is not None:
            self.telemetry.record_model_call(call, time.monotonic() - started)
        return ProviderResult(safe_parsed, outcome.refusal, call)


class DeterministicFakeProvider(FakeModelProvider):
    """Unlimited offline provider for the Compose walkthrough fixture."""

    def __init__(
        self,
        telemetry: WorkerTelemetry | None = None,
        *,
        capture_policy: ModelPayloadCapturePolicy | None = None,
        delay_seconds: float = 0,
    ) -> None:
        if not 0 <= delay_seconds <= 30:
            raise ValueError("fake provider delay must be between 0 and 30 seconds")
        self.delay_seconds = delay_seconds
        super().__init__(
            [],
            telemetry=telemetry,
            capture_policy=capture_policy,
        )

    def complete(self, request: ModelRequest) -> ProviderResult:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        compiled_input = request.compiled_input()
        compiled_request = replace(
            request,
            model_input=compiled_input.value,
        )
        if compiled_request.purpose.startswith("materiality"):
            facts = compiled_request.model_input.get("evidence", {}).get("facts", [])
            evidence_ids = [str(fact["evidence_id"]) for fact in facts]
            rules = (
                (
                    "python_compatibility",
                    "computed.requires_python_diff.after",
                    "The candidate changes the declared Python compatibility range.",
                ),
                (
                    "dependency_contract",
                    "computed.requires_dist_diff.",
                    "The candidate changes a declared dependency requirement.",
                ),
                (
                    "release_withdrawal",
                    "computed.yank_diff.after",
                    "The candidate changes the release withdrawal state.",
                ),
                (
                    "security_advisory",
                    "candidate.vulnerabilities.",
                    "The candidate has structured vulnerability evidence.",
                ),
                (
                    "packaging_metadata",
                    "computed.files_diff.added.",
                    "The candidate changes the published distribution artifacts.",
                ),
            )
            change_types: list[str] = []
            claims: list[Json] = []
            for change_type, prefix, statement in rules:
                evidence_id = next((item for item in evidence_ids if item.startswith(prefix)), None)
                if evidence_id is None:
                    continue
                change_types.append(change_type)
                claims.append(
                    {
                        "statement": statement,
                        "evidence_ids": [evidence_id],
                        "support": "direct",
                        "conditions": [],
                    }
                )
            if not claims and evidence_ids:
                change_types = ["unknown"]
                claims = [
                    {
                        "statement": "The candidate changes a model-visible evidence fact.",
                        "evidence_ids": [evidence_ids[0]],
                        "support": "direct",
                        "conditions": [],
                    }
                ]
            outcome = FakeOutcome(
                parsed={
                    "decision": "substantive" if claims else "insufficient_evidence",
                    "change_types": change_types,
                    "claims": claims,
                    "missing_evidence": []
                    if claims
                    else ["No model-visible evidence facts were supplied"],
                    "confidence": 0.9,
                }
            )
        elif compiled_request.purpose in {
            "applicability_assessment",
            "applicability_correction",
        }:
            accepted = compiled_request.model_input.get("accepted_materiality", {})
            evidence_ids = [
                str(evidence_id)
                for claim in accepted.get("claims", [])
                for evidence_id in claim.get("evidence_ids", [])
            ]
            release_context = compiled_request.model_input.get("release_context", {})
            package = str(release_context.get("package", "the package"))
            candidate_version = str(release_context.get("candidate_version", "unknown"))
            release_name = f"{package} {candidate_version}"
            consumer_scenarios = []
            if evidence_ids:
                consumer_scenarios.append(
                    {
                        "impact_kind": "metadata",
                        "package": package,
                        "candidate_version": candidate_version,
                        "consumer_trigger": f"A consumer selects {release_name}.",
                        "changed_behavior": (f"{release_name} changes a declared package rule."),
                        "observable_outcome": (
                            "Installation may resolve different compatibility or dependency "
                            "requirements."
                        ),
                        "verification": f"Test {release_name} in the intended Python environment.",
                        "evidence_ids": [evidence_ids[0]],
                        "conditions": [f"Release == {release_name}"],
                    }
                )
            outcome = FakeOutcome(
                parsed={
                    "assessment": "The accepted evidence supports analysis for this explicitly monitored package.",
                    "consumer_scenarios": consumer_scenarios,
                    "confidence": 0.9,
                    "limitations": [
                        "Configured environment profiles only",
                    ],
                }
            )
        else:
            release = compiled_request.model_input.get("release_identity", {})
            package = str(release.get("package", "the package"))
            candidate = str(release.get("candidate_version", "unknown"))
            accepted = compiled_request.model_input.get("accepted_claims", [])
            evidence_ids = [
                str(evidence_id)
                for claim in accepted
                for evidence_id in claim.get("evidence_ids", [])
            ]
            evidence_catalog = compiled_request.model_input.get("evidence_catalog", [])
            primary_evidence = evidence_ids[0] if evidence_ids else ""
            if "requires_python" in primary_evidence:
                changed_value = "Requires-Python >=3.10"
            elif "requires_dist" in primary_evidence:
                changed_value = "a new dependency version constraint"
            else:
                changed_value = next(
                    (
                        str(item.get("value"))
                        for item in evidence_catalog
                        if item.get("evidence_id") == primary_evidence
                    ),
                    "a declared package rule",
                )
            outcome = FakeOutcome(
                parsed={
                    "decision": "publishable_summary",
                    "impact_type": "runtime_compatibility",
                    "headline": f"Compatibility change can block {package} {candidate}",
                    "affected_if": f"You install {package} {candidate} in an environment outside its declared compatibility range.",
                    "what_happens": f"{package} {candidate} declares {changed_value}, so an incompatible installer rejects the release.",
                    "not_affected_if": "Your environment satisfies the newly declared package requirement.",
                    "recommended_action": "Confirm the target environment before accepting the upgrade.",
                    "verification": f"Run python -m pip install --dry-run {package}=={candidate} in the target environment.",
                    "reach_summary": "This package is explicitly monitored; the analysis does not measure affected users.",
                    "evidence_ids": evidence_ids[:1],
                    "limitations": [
                        "The analysis covers configured environments for this monitored package only."
                    ],
                }
            )
        self.outcomes.append(outcome)
        return self._complete_compiled(compiled_request, compiled_input)


def _visible_output(response: Any) -> tuple[Json | None, str | None]:
    refusal: str | None = None
    for output in getattr(response, "output", []) or []:
        if getattr(output, "type", None) != "message":
            continue
        for item in getattr(output, "content", []) or []:
            if getattr(item, "type", None) == "refusal":
                refusal = str(getattr(item, "refusal", "refused"))
    if refusal:
        return None, refusal
    text = getattr(response, "output_text", None)
    if not text:
        return None, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"_malformed_output": str(text)}, None
    if not isinstance(value, dict):
        return {"_malformed_output": value}, None
    return value, None


def _usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        cached_input_tokens=int(getattr(input_details, "cached_tokens", 0) or 0),
        cache_write_tokens=int(getattr(input_details, "cache_write_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        reasoning_tokens=int(getattr(output_details, "reasoning_tokens", 0) or 0),
    )


def _public_dump(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float, list, dict)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)


def _correlation(response: Any) -> tuple[str | None, float | None]:
    request_id = getattr(response, "_request_id", None)
    headers = getattr(getattr(response, "_response", None), "headers", {}) or {}
    request_id = request_id or headers.get("x-request-id")
    raw_ms = headers.get("openai-processing-ms")
    try:
        processing_ms = float(raw_ms) if raw_ms is not None else None
    except (TypeError, ValueError):
        processing_ms = None
    return request_id, processing_ms


def _retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status in {408, 409, 429} or (isinstance(status, int) and status >= 500):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
    }


def _error_class(exc: Exception) -> str:
    return type(exc).__name__.lower()


def _prompt_cache_key(request: ModelRequest) -> str:
    contract_hash = sha256_json(
        {
            "namespace": request.cache_namespace,
            "model": request.model,
            "instructions": request.instructions,
            "output_schema_name": request.output_schema_name,
            "output_schema": request.output_schema,
        }
    ).removeprefix("sha256:")
    return f"pypi-{request.cache_namespace}-{contract_hash[:32]}"


def _estimate_cost(usage: TokenUsage, *, model: str, service_tier: str) -> float:
    # USD per million tokens from the versioned price table. Reasoning tokens are
    # already included in output_tokens and cache reads/writes are disjoint input classes.
    prices = {
        ("gpt-5.6-sol", "default"): (5.0, 0.5, 6.25, 30.0),
        ("gpt-5.6-sol", "flex"): (2.5, 0.25, 3.125, 15.0),
        ("gpt-5.6-terra", "default"): (2.5, 0.25, 3.125, 15.0),
        ("gpt-5.6-terra", "flex"): (1.25, 0.125, 1.5625, 7.5),
    }
    input_rate, cached_rate, write_rate, output_rate = prices.get(
        (model, service_tier),
        prices[("gpt-5.6-sol", "default")],
    )
    uncached = max(
        usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens,
        0,
    )
    total = (
        uncached * input_rate
        + usage.cached_input_tokens * cached_rate
        + usage.cache_write_tokens * write_rate
        + usage.output_tokens * output_rate
    )
    return round(total / 1_000_000, 8)


def _failed_call(
    model_call_id: str,
    request: ModelRequest,
    persisted_request: Json | None,
    request_sha256: str,
    attempts: tuple[PhysicalAttempt, ...],
    span_id: str | None,
) -> ModelCallRecord:
    return ModelCallRecord(
        model_call_id=model_call_id,
        purpose=request.purpose,
        requested_model=request.model,
        returned_model=None,
        requested_service_tier=request.service_tier.value,
        returned_service_tier=None,
        reasoning_effort=request.reasoning_effort.value,
        request_payload=persisted_request,
        response_payload=None,
        request_sha256=request_sha256,
        response_sha256=None,
        usage=TokenUsage(),
        attempts=attempts,
        outcome="error",
        estimated_cost_usd=0,
        span_id=span_id,
    )
