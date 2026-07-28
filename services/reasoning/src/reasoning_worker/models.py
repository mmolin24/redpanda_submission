"""Define the typed domain and wire models used by the reasoning pipeline."""

from __future__ import annotations

import re
import secrets
from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, cast
from urllib.parse import urlsplit

Json = dict[str, Any]
AnalysisMethod = Literal["deterministic", "model_assisted"]
ReasoningComplexity = Literal["simple", "moderate", "complex"]
EvidenceCollectionStatus = Literal["complete", "partial", "failed"]
EvidenceSource = Literal["baseline", "candidate", "computed", "context"]
ModelPurpose = Literal[
    "materiality_assessment",
    "materiality_correction",
    "materiality_review",
    "applicability_assessment",
    "applicability_correction",
    "customer_impact_summary",
    "customer_impact_correction",
]
ImpactKind = Literal["metadata", "runtime"]
FailureStage = Literal[
    "ingestion",
    "enrichment",
    "routing",
    "materiality",
    "applicability",
    "customer_impact",
    "publication",
    "sink",
]


def utc_now() -> str:
    """Return the current UTC time in normalized RFC 3339 form."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Decision(StrEnum):
    """Describe the materiality decision for a release."""

    SUBSTANTIVE = "substantive"
    NON_SUBSTANTIVE = "non_substantive"
    INSUFFICIENT = "insufficient_evidence"


class Disposition(StrEnum):
    """Describe the terminal customer-publication outcome."""

    PRERELEASE = "prerelease_observed"
    NON_SUBSTANTIVE = "non_substantive"
    INSUFFICIENT = "insufficient_evidence"
    REFUSED = "refused"
    PUBLISHABLE = "publishable"
    LOW_CONFIDENCE = "suppressed_low_confidence"
    VALIDATION_FAILURE = "suppressed_validation_failure"


class Priority(StrEnum):
    """Order release analyses for bounded processing."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    SKIP = "skip"


class ServiceTier(StrEnum):
    """Select the provider service tier for model-assisted work."""

    DEFAULT = "default"
    FLEX = "flex"


class ReasoningEffort(StrEnum):
    """Select the bounded model reasoning-effort level."""

    LOW = "low"
    MEDIUM = "medium"


CHANGE_TYPES = {
    "dependency_contract",
    "python_compatibility",
    "platform_installability",
    "release_availability",
    "release_withdrawal",
    "security_advisory",
    "packaging_metadata",
    "runtime_behavior_unobservable",
    "unknown",
}

_EVENT_KEY_PATTERN = re.compile(r"^pypi:[a-z0-9]+(?:-[a-z0-9]+)*:.+$")
_NORMALIZED_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_TRACE_FLAGS_PATTERN = re.compile(r"^[0-9a-f]{2}$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])[Tt]"
    r"(?:[01]\d|2[0-3]):[0-5]\d:(?:[0-5]\d|60)"
    r"(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)
_URI_CHARACTER_PATTERN = re.compile(r"^[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%]+$")
_URI_PATH_PATTERN = re.compile(r"^(?:/[A-Za-z0-9\-._~!$&'()*+,;=:@%]*)*$")
_URI_QUERY_FRAGMENT_PATTERN = re.compile(r"^[A-Za-z0-9\-._~!$&'()*+,;=:@%/?]*$")
_INVALID_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")


class ReleaseEventContractError(ValueError):
    """A release-topic value does not satisfy release-event.v1."""


def _contract_error(path: str) -> ReleaseEventContractError:
    return ReleaseEventContractError(
        f"release event field {path} does not satisfy release-event.v1"
    )


def _require_object(
    value: object,
    *,
    path: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Json:
    if not isinstance(value, dict):
        raise _contract_error(path)
    actual = set(value)
    if not required.issubset(actual) or not actual.issubset(required | optional):
        raise _contract_error(path)
    return value


def _require_string(
    value: object,
    *,
    path: str,
    minimum: int = 0,
    maximum: int | None = None,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise _contract_error(path)
    if len(value) < minimum or (maximum is not None and len(value) > maximum):
        raise _contract_error(path)
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _contract_error(path)
    return value


def _require_rfc3339(value: object, *, path: str) -> str:
    rendered = _require_string(value, path=path)
    if _RFC3339_PATTERN.fullmatch(rendered) is None:
        raise _contract_error(path)
    has_leap_second = rendered[17:19] == "60"
    calendar_check = rendered[:17] + "59" + rendered[19:] if has_leap_second else rendered
    normalized = (
        calendar_check[:-1] + "+00:00" if calendar_check.endswith(("Z", "z")) else calendar_check
    )
    try:
        parsed = datetime.fromisoformat(normalized)
        utc = parsed.astimezone(UTC) if has_leap_second else None
    except (OverflowError, ValueError) as exc:
        raise _contract_error(path) from exc
    if parsed.tzinfo is None:
        raise _contract_error(path)
    if has_leap_second:
        if utc is None or (
            (utc.hour, utc.minute, utc.second) != (23, 59, 59)
            or utc.day != monthrange(utc.year, utc.month)[1]
        ):
            raise _contract_error(path)
    return rendered


def _require_pypi_uri(value: object, *, path: str) -> str:
    rendered = _require_string(value, path=path)
    if (
        _URI_CHARACTER_PATTERN.fullmatch(rendered) is None
        or _INVALID_PERCENT_ESCAPE_PATTERN.search(rendered) is not None
    ):
        raise _contract_error(path)
    try:
        parsed = urlsplit(rendered)
    except ValueError as exc:
        raise _contract_error(path) from exc
    if (
        not rendered.startswith("https://pypi.org/project/")
        or parsed.scheme != "https"
        or parsed.netloc != "pypi.org"
        or _URI_PATH_PATTERN.fullmatch(parsed.path) is None
        or _URI_QUERY_FRAGMENT_PATTERN.fullmatch(parsed.query) is None
        or _URI_QUERY_FRAGMENT_PATTERN.fullmatch(parsed.fragment) is None
    ):
        raise _contract_error(path)
    return rendered


@dataclass(frozen=True)
class PackageRef:
    """Identify a package by source and normalized names."""

    name: str
    normalized_name: str


@dataclass(frozen=True)
class ReleaseRef:
    """Identify a version and its public PyPI release metadata."""

    version: str
    published_at: str | None
    url: str


@dataclass(frozen=True)
class ReleaseEvent:
    """Represent the validated release record consumed from Redpanda."""

    event_key: str
    source: str
    package: PackageRef
    release: ReleaseRef
    ingested_at: str
    observability: Json = field(default_factory=dict)
    schema_version: str = "release-event.v1"

    @classmethod
    def from_dict(cls, value: object) -> ReleaseEvent:
        root = _require_object(
            value,
            path="$",
            required=frozenset(
                {
                    "schema_version",
                    "event_key",
                    "source",
                    "package",
                    "release",
                    "ingested_at",
                    "observability",
                }
            ),
        )
        if root["schema_version"] != "release-event.v1":
            raise _contract_error("$.schema_version")
        event_key = _require_string(
            root["event_key"],
            path="$.event_key",
            maximum=512,
            pattern=_EVENT_KEY_PATTERN,
        )
        if root["source"] != "pypi-rss-updates":
            raise _contract_error("$.source")

        package = _require_object(
            root["package"],
            path="$.package",
            required=frozenset({"name", "normalized_name"}),
        )
        package_name = _require_string(
            package["name"],
            path="$.package.name",
            minimum=1,
            maximum=256,
        )
        normalized_name = _require_string(
            package["normalized_name"],
            path="$.package.normalized_name",
            maximum=256,
            pattern=_NORMALIZED_NAME_PATTERN,
        )

        release = _require_object(
            root["release"],
            path="$.release",
            required=frozenset({"version", "published_at", "url"}),
        )
        release_version = _require_string(
            release["version"],
            path="$.release.version",
            minimum=1,
            maximum=128,
        )
        published_at = release["published_at"]
        if published_at is not None:
            published_at = _require_rfc3339(
                published_at,
                path="$.release.published_at",
            )
        release_url = _require_pypi_uri(
            release["url"],
            path="$.release.url",
        )

        ingested_at = _require_rfc3339(
            root["ingested_at"],
            path="$.ingested_at",
        )
        observability = _require_object(
            root["observability"],
            path="$.observability",
            required=frozenset(
                {
                    "processing_attempt_id",
                    "analysis_trace_id",
                    "analysis_span_id",
                    "trace_flags",
                    "stage_summary",
                }
            ),
            optional=frozenset({"tracestate"}),
        )
        _require_string(
            observability["processing_attempt_id"],
            path="$.observability.processing_attempt_id",
            minimum=1,
            maximum=64,
        )
        _require_string(
            observability["analysis_trace_id"],
            path="$.observability.analysis_trace_id",
            pattern=_TRACE_ID_PATTERN,
        )
        _require_string(
            observability["analysis_span_id"],
            path="$.observability.analysis_span_id",
            pattern=_SPAN_ID_PATTERN,
        )
        _require_string(
            observability["trace_flags"],
            path="$.observability.trace_flags",
            pattern=_TRACE_FLAGS_PATTERN,
        )
        tracestate = observability.get("tracestate")
        if tracestate is not None:
            _require_string(
                tracestate,
                path="$.observability.tracestate",
                maximum=512,
            )
        stage_summary = observability["stage_summary"]
        if (
            not isinstance(stage_summary, list)
            or not stage_summary
            or not all(isinstance(stage, dict) for stage in stage_summary)
        ):
            raise _contract_error("$.observability.stage_summary")

        event = cls(
            event_key=event_key,
            source="pypi-rss-updates",
            package=PackageRef(package_name, normalized_name),
            release=ReleaseRef(
                release_version,
                cast(str | None, published_at),
                release_url,
            ),
            ingested_at=ingested_at,
            observability=dict(observability),
        )
        if event.event_key != f"pypi:{event.package.normalized_name}:{event.release.version}":
            raise _contract_error("$.event_key")
        return event

    def to_dict(self) -> Json:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceFact:
    """Attach a stable citation identity and source to one evidence value."""

    evidence_id: str
    value: Any
    source: EvidenceSource


@dataclass(frozen=True)
class EvidenceBundle:
    """Contain normalized candidate, baseline, computed, and contextual evidence."""

    bundle_id: str
    event_key: str
    package: str
    baseline: Json
    candidate: Json
    computed: Json
    context: Json
    provenance: tuple[Json, ...]
    collection_status: EvidenceCollectionStatus
    facts: tuple[EvidenceFact, ...]
    sanitization: Json = field(default_factory=dict)
    schema_version: str = "evidence-bundle.v1"

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(f.evidence_id for f in self.facts)

    @property
    def vulnerability_ids(self) -> frozenset[str]:
        return frozenset(
            f.evidence_id
            for f in self.facts
            if f.evidence_id.startswith("candidate.vulnerabilities.")
        )

    def model_view(self) -> Json:
        """Build the only evidence view allowed across the materiality boundary."""
        from .model_input import bound_collection, summarize_oversized_value
        from .sanitization import sanitize

        complete_facts = [sanitize(asdict(fact)) for fact in self.facts]
        facts = [
            {
                **fact,
                "value": summarize_oversized_value(fact["value"]),
            }
            for fact in complete_facts
        ]
        selection = bound_collection(
            facts,
            selection_key=lambda fact: (
                {
                    "computed": 0,
                    "candidate": 1,
                    "context": 2,
                    "baseline": 3,
                }.get(str(fact.get("source")), 4),
                (
                    fact.get("value", {}).get("selection_priority", 100)
                    if isinstance(fact.get("value"), dict)
                    else 100
                ),
                (
                    str(fact.get("value", {}).get("path", ""))
                    if isinstance(fact.get("value"), dict)
                    else ""
                ),
                str(fact.get("evidence_id")),
            ),
            manifest_values=complete_facts,
        )
        return {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "package": self.package,
            "baseline_version": self.baseline.get("version"),
            "candidate_version": self.candidate.get("version"),
            "collection_status": self.collection_status,
            "sanitization": self.sanitization,
            "facts": list(selection.items),
            "facts_summary": selection.summary,
            "missing": self.computed.get("missing", []),
        }

    def to_dict(self) -> Json:
        return asdict(self)


@dataclass(frozen=True)
class RoutingEnvelope:
    """Record the deterministic routing decision and model configuration."""

    processing_priority: Priority
    analysis_eligibility: Literal[
        "model",
        "observe_only",
        "deterministic_non_substantive",
        "deterministic_impact",
    ]
    reasoning_complexity: ReasoningComplexity
    reasons: tuple[str, ...]
    service_tier: ServiceTier | None
    model: str | None
    reasoning_effort: ReasoningEffort | None
    policy_version: str = "routing-v3"


@dataclass(frozen=True)
class Claim:
    """Represent one materiality claim grounded in supplied evidence."""

    statement: str
    evidence_ids: tuple[str, ...]
    support: Literal["direct", "conditional"]
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterialityResult:
    """Capture whether supported evidence establishes a substantive change."""

    decision: Decision
    change_types: tuple[str, ...]
    claims: tuple[Claim, ...]
    missing_evidence: tuple[str, ...]
    confidence: float

    @classmethod
    def from_dict(cls, value: Json) -> MaterialityResult:
        return cls(
            decision=Decision(value["decision"]),
            change_types=tuple(str(x) for x in value.get("change_types", [])),
            claims=tuple(
                Claim(
                    statement=str(c["statement"]),
                    evidence_ids=tuple(str(x) for x in c.get("evidence_ids", [])),
                    support=c["support"],
                    conditions=tuple(str(x) for x in c.get("conditions", [])),
                )
                for c in value.get("claims", [])
            ),
            missing_evidence=tuple(str(x) for x in value.get("missing_evidence", [])),
            confidence=float(value["confidence"]),
        )

    def to_dict(self) -> Json:
        return asdict(self)


@dataclass(frozen=True)
class ConsumerScenario:
    """Describe one evidence-grounded condition and customer consequence."""

    impact_kind: ImpactKind
    package: str
    candidate_version: str
    consumer_trigger: str
    changed_behavior: str
    observable_outcome: str
    verification: str
    evidence_ids: tuple[str, ...]
    conditions: tuple[str, ...]


@dataclass(frozen=True)
class ApplicabilityResult:
    """Capture concrete consumer scenarios for a material release change."""

    assessment: str
    consumer_scenarios: tuple[ConsumerScenario, ...]
    confidence: float
    limitations: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Json) -> ApplicabilityResult:
        return cls(
            assessment=str(value["assessment"]),
            consumer_scenarios=tuple(
                ConsumerScenario(
                    impact_kind=item["impact_kind"],
                    package=str(item["package"]),
                    candidate_version=str(item["candidate_version"]),
                    consumer_trigger=str(item["consumer_trigger"]),
                    changed_behavior=str(item["changed_behavior"]),
                    observable_outcome=str(item["observable_outcome"]),
                    verification=str(item["verification"]),
                    evidence_ids=tuple(str(x) for x in item.get("evidence_ids", [])),
                    conditions=tuple(str(x) for x in item.get("conditions", [])),
                )
                for item in value.get("consumer_scenarios", [])
            ),
            confidence=float(value["confidence"]),
            limitations=tuple(str(x) for x in value.get("limitations", [])),
        )

    def to_dict(self) -> Json:
        return asdict(self)


IMPACT_TYPES = {
    "install_block",
    "dependency_conflict",
    "dependency_upgrade",
    "runtime_compatibility",
    "platform_installability",
    "release_availability",
    "release_withdrawal",
    "security_signal",
    "other",
}


@dataclass(frozen=True)
class DecisionCard:
    """Present one concise condition and recommended action."""

    headline: str
    applies_when: str
    action: str
    source_scenario_id: str

    @classmethod
    def from_dict(cls, value: Json) -> DecisionCard:
        return cls(
            headline=str(value["headline"]),
            applies_when=str(value["applies_when"]),
            action=str(value["action"]),
            source_scenario_id=str(value["source_scenario_id"]),
        )


@dataclass(frozen=True)
class CustomerImpactSummary:
    """Present the bounded customer-facing conclusion for a release."""

    decision: Literal["publishable_summary", "insufficient_summary"]
    impact_type: str
    headline: str
    affected_if: str
    what_happens: str
    not_affected_if: str
    recommended_action: str
    verification: str
    reach_summary: str
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    decision_card: DecisionCard | None = None

    @classmethod
    def from_dict(cls, value: Json) -> CustomerImpactSummary:
        decision_card = value.get("decision_card")
        return cls(
            decision=value["decision"],
            impact_type=str(value["impact_type"]),
            headline=str(value["headline"]),
            affected_if=str(value["affected_if"]),
            what_happens=str(value["what_happens"]),
            not_affected_if=str(value["not_affected_if"]),
            recommended_action=str(value["recommended_action"]),
            verification=str(value["verification"]),
            reach_summary=str(value["reach_summary"]),
            evidence_ids=tuple(str(item) for item in value.get("evidence_ids", [])),
            limitations=tuple(str(item) for item in value.get("limitations", [])),
            decision_card=(
                DecisionCard.from_dict(decision_card) if isinstance(decision_card, dict) else None
            ),
        )

    def to_dict(self) -> Json:
        return asdict(self)


@dataclass(frozen=True)
class TokenUsage:
    """Record provider token accounting for one logical model call."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class PhysicalAttempt:
    """Record one provider HTTP attempt within a logical model call."""

    attempt_id: str
    client_request_id: str
    attempt_number: int
    outcome: str
    started_at: str
    completed_at: str
    latency_ms: float
    response_id: str | None = None
    openai_request_id: str | None = None
    openai_processing_ms: float | None = None
    error_class: str | None = None


@dataclass(frozen=True)
class ModelCallRecord:
    """Persist the bounded request, result, attempts, usage, and cost metadata."""

    model_call_id: str
    purpose: ModelPurpose
    requested_model: str
    returned_model: str | None
    requested_service_tier: str
    returned_service_tier: str | None
    reasoning_effort: str
    request_payload: Json | None
    response_payload: Json | None
    request_sha256: str
    response_sha256: str | None
    usage: TokenUsage
    attempts: tuple[PhysicalAttempt, ...]
    outcome: str
    estimated_cost_usd: float
    span_id: str | None = None
    price_table_version: str = "openai-2026-07-22-cache-aware"


@dataclass(frozen=True)
class TraceContext:
    """Carry W3C trace context across Redpanda service boundaries."""

    traceparent: str | None = None
    tracestate: str | None = None

    @property
    def trace_id(self) -> str | None:
        if not self.traceparent:
            return None
        parts = self.traceparent.split("-")
        return parts[1] if len(parts) == 4 else None

    @property
    def span_id(self) -> str | None:
        parts = self.traceparent.split("-") if self.traceparent else []
        return parts[2] if len(parts) == 4 else None

    @property
    def trace_flags(self) -> str:
        parts = self.traceparent.split("-") if self.traceparent else []
        return parts[3] if len(parts) == 4 else "01"

    def ensured(self) -> TraceContext:
        if self.trace_id and self.span_id:
            return self
        return TraceContext(
            traceparent=f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01",
            tracestate=self.tracestate,
        )

    def to_observability(self, processing_attempt_id: str, stage_summary: list[Json]) -> Json:
        value = self.ensured()
        return {
            "processing_attempt_id": processing_attempt_id,
            "analysis_trace_id": value.trace_id,
            "analysis_span_id": value.span_id,
            "trace_flags": value.trace_flags,
            "tracestate": value.tracestate,
            "stage_summary": stage_summary,
        }


@dataclass(frozen=True)
class TerminalMetadata:
    """Collect attempt identity, timing, model calls, and trace metadata."""

    analysis_version: str
    processing_attempt_id: str
    model_calls: tuple[ModelCallRecord, ...]
    started_at: str
    completed_at: str
    trace_context: TraceContext
    stage_summary: tuple[Json, ...]


@dataclass(frozen=True)
class Finding:
    """Represent one complete terminal release-triage finding."""

    finding_id: str
    event_key: str
    source_event: Json
    disposition: Disposition
    analysis_method: AnalysisMethod
    package: str
    baseline_version: str | None
    candidate_version: str
    gate_results: Json
    evidence_bundle: Json
    routing: Json
    analysis_metadata: Json
    publishable: bool
    observability: Json
    schema_version: str = "finding.v1"

    def to_dict(self) -> Json:
        return asdict(self)


@dataclass(frozen=True)
class FailureRecord:
    """Represent one bounded terminal processing failure occurrence."""

    failure_id: str
    failure_fingerprint: str
    event_key: str | None
    stage: FailureStage
    error_class: str
    message: str
    retryable: bool
    attempt_count: int
    first_failed_at: str
    last_failed_at: str
    payload: Json
    observability: Json = field(default_factory=dict)
    next_retry_at: str | None = None
    schema_version: str = "failure.v1"

    def to_dict(self) -> Json:
        return asdict(self)
