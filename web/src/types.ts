export type JsonObject = Record<string, unknown>;

export interface FindingSummary {
  finding_id: string;
  analysis_version: string;
  package_name: string;
  baseline_version: string | null;
  candidate_version: string;
  change_types: string[];
  assessment: string | null;
  confidence: number | null;
  processing_priority: string | null;
  disposition: string;
  publishable: boolean;
  analysis_method: "deterministic" | "model_assisted";
  event_published_at: string | null;
  ingested_at: string | null;
  published_at: string | null;
  evidence_partial: boolean;
  analysis_trace_id: string | null;
}

export interface FindingPage {
  items: FindingSummary[];
  meta: { page: number; page_size: number; total: number; pages: number };
}

export interface FindingDetail extends FindingSummary {
  evidence_bundle: JsonObject;
  limitations: unknown;
  gate_results: JsonObject;
  model_calls: JsonObject[];
  source_event: JsonObject;
  observability: JsonObject;
}

export interface ReleaseObservation {
  event_key: string;
  package_name: string;
  version: string;
  event_published_at: string | null;
  ingested_at: string;
  disposition: string | null;
  analysis_method: "deterministic" | "model_assisted" | null;
  publishable: boolean | null;
}

export interface StatsResponse {
  materiality_counts: Record<string, number>;
  disposition_counts: Record<string, number>;
  release_event_count: number;
  latest_release: ReleaseObservation | null;
  unresolved_failures: number;
  last_ingestion_at: string | null;
  estimated_cost_usd: number;
  price_table_date: string | null;
}

export interface OpsSummary {
  status: "healthy" | "degraded" | "unknown";
  freshness: {
    source_mode: "fixture" | "history" | "live";
    status: "unknown" | "waiting" | "fixture" | "historical" | "recent" | "quiet";
    last_ingestion_at: string | null;
    age_seconds: number | null;
    detail: string;
  };
  attention?: {
    status: "unknown" | "clear" | "attention";
    unresolved_failures: number | null;
    detail: string;
  };
  components: Record<string, { status: string; detail?: string }>;
  recent_dispositions: Record<string, number>;
  unresolved_failures: number | null;
  openai: JsonObject;
}

export interface TraceStage {
  stage: string;
  outcome: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  detail: string | null;
}

export interface TraceSummary {
  finding_id: string;
  analysis_trace_id: string;
  processing_attempt_id: string;
  stage_summary: TraceStage[];
  model_calls: JsonObject[];
  grafana_urls: Record<string, string>;
}
