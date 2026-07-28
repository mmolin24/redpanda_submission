"""Define API response models for findings, health, operations, and traces."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JsonObject = dict[str, Any]


class HealthResponse(BaseModel):
    """Report API and database readiness."""

    status: Literal["healthy", "degraded"]
    database: Literal["ready", "unavailable"]


class FindingSummary(BaseModel):
    """Represent one finding in a filtered result page."""

    finding_id: str
    analysis_version: str
    package_name: str
    baseline_version: str | None = None
    candidate_version: str
    change_types: list[str] = Field(default_factory=list)
    assessment: str | None = None
    confidence: float | None = None
    processing_priority: str | None = None
    disposition: str
    publishable: bool
    analysis_method: Literal["deterministic", "model_assisted"]
    event_published_at: datetime | None = None
    ingested_at: datetime | None = None
    published_at: datetime | None = None
    evidence_partial: bool = False
    analysis_trace_id: str | None = None


class PageMeta(BaseModel):
    """Describe pagination for a finding collection."""

    page: int
    page_size: int
    total: int
    pages: int


class FindingPage(BaseModel):
    """Return one page of summarized findings."""

    items: list[FindingSummary]
    meta: PageMeta


class ReleaseObservation(BaseModel):
    """Describe the latest persisted release and its analysis state."""

    event_key: str
    package_name: str
    version: str
    event_published_at: datetime | None = None
    ingested_at: datetime
    disposition: str | None = None
    analysis_method: Literal["deterministic", "model_assisted"] | None = None
    publishable: bool | None = None


class FindingDetail(BaseModel):
    """Expose the complete customer-facing finding and supporting evidence."""

    model_config = ConfigDict(extra="allow")

    finding_id: str
    analysis_version: str
    package_name: str
    baseline_version: str | None = None
    candidate_version: str
    change_types: list[str] = Field(default_factory=list)
    assessment: str | None = None
    confidence: float | None = None
    processing_priority: str | None = None
    disposition: str
    analysis_method: Literal["deterministic", "model_assisted"]
    published_at: datetime | None = None
    evidence_bundle: JsonObject = Field(default_factory=dict)
    limitations: Any = Field(default_factory=list)
    gate_results: JsonObject = Field(default_factory=dict)
    model_calls: list[JsonObject] = Field(default_factory=list)
    source_event: JsonObject = Field(default_factory=dict)
    observability: JsonObject = Field(default_factory=dict)


class PackageHistory(BaseModel):
    """Group persisted releases and findings for one normalized package."""

    normalized_name: str
    findings: list[FindingSummary] = Field(default_factory=list)
    releases: list[JsonObject] = Field(default_factory=list)


class StatsResponse(BaseModel):
    """Summarize persisted throughput, outcomes, failures, and model cost."""

    materiality_counts: JsonObject = Field(default_factory=dict)
    disposition_counts: JsonObject = Field(default_factory=dict)
    release_event_count: int = 0
    latest_release: ReleaseObservation | None = None
    unresolved_failures: int = 0
    last_ingestion_at: datetime | None = None
    estimated_cost_usd: float = 0
    price_table_date: str | None = None


class ComponentStatus(BaseModel):
    """Report the bounded health view for one runtime component."""

    status: Literal["healthy", "degraded", "unknown"]
    detail: str | None = None
    observed_at: datetime | None = None


class DataFreshness(BaseModel):
    """Explain whether persisted releases are current for the source mode."""

    source_mode: Literal["fixture", "history", "live"] = "fixture"
    status: Literal["unknown", "waiting", "fixture", "historical", "recent", "quiet"] = "waiting"
    last_ingestion_at: datetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    detail: str = "No release data has been persisted yet."


class FailureAttention(BaseModel):
    """Summarize unresolved failures that need operator attention."""

    status: Literal["unknown", "clear", "attention"] = "clear"
    unresolved_failures: int | None = Field(default=0, ge=0)
    detail: str = "No unresolved processing failures."


class OpsSummary(BaseModel):
    """Combine freshness, failures, components, and model usage for operators."""

    status: Literal["healthy", "degraded", "unknown"]
    freshness: DataFreshness = Field(default_factory=DataFreshness)
    attention: FailureAttention = Field(default_factory=FailureAttention)
    components: dict[str, ComponentStatus] = Field(default_factory=dict)
    recent_dispositions: JsonObject = Field(default_factory=dict)
    unresolved_failures: int | None = Field(default=0, ge=0)
    openai: JsonObject = Field(default_factory=dict)


class TraceStage(BaseModel):
    """Describe one observable processing stage for a release attempt."""

    stage: str
    outcome: str = "completed"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = None
    detail: str | None = None


class TraceAttempt(BaseModel):
    """Describe the latest persisted attempt and its processing stages."""

    model_config = ConfigDict(extra="allow")

    processing_attempt_id: str
    outcome: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    stages: list[TraceStage] = Field(default_factory=list)


class TraceSummary(BaseModel):
    """Link a finding to its trace, model calls, and Grafana views."""

    finding_id: str
    analysis_trace_id: str
    processing_attempt_id: str
    stage_summary: list[TraceStage] = Field(default_factory=list)
    model_calls: list[JsonObject] = Field(default_factory=list)
    grafana_urls: dict[str, str]
