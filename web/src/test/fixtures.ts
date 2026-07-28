import type {
  FindingDetail,
  FindingPage,
  FindingSummary,
  StatsResponse,
  TraceSummary,
} from "../types";
import { vi } from "vitest";

export const finding: FindingSummary = {
  finding_id: "finding-1",
  analysis_version: "analysis-v1",
  package_name: "requests",
  baseline_version: "2.31.0",
  candidate_version: "2.32.0",
  change_types: ["dependency_contract"],
  assessment: "A tighter dependency constraint may affect consumers.",
  confidence: 0.86,
  processing_priority: "high",
  disposition: "publishable",
  publishable: true,
  analysis_method: "model_assisted",
  event_published_at: "2026-07-20T12:34:56Z",
  ingested_at: "2026-07-20T12:35:10Z",
  published_at: "2026-07-21T12:00:00Z",
  evidence_partial: false,
  analysis_trace_id: "a".repeat(32),
};

export const findingPage: FindingPage = {
  items: [finding],
  meta: { page: 1, page_size: 25, total: 1, pages: 1 },
};
export const insufficientFinding: FindingSummary = {
  ...finding,
  finding_id: "finding-cffi",
  package_name: "cffi",
  baseline_version: "2.0.0",
  candidate_version: "2.1.0",
  assessment: "The configured profiles do not include the affected Python 3.9 scenario.",
  disposition: "insufficient_evidence",
  publishable: false,
  confidence: 0.99,
};
export const findingDetail: FindingDetail = {
  ...finding,
  evidence_bundle: { items: [{ evidence_id: "dep-1", before: "<3", after: "<2" }] },
  limitations: ["Configured environments only"],
  gate_results: {
    materiality: {
      decision: "substantive",
      claims: [
        {
          support: "conditional",
          statement: "The dependency constraint changed.",
          conditions: ["A consumer resolves the affected dependency range."],
          evidence_ids: ["dep-1"],
        },
      ],
    },
    applicability: {
      impact_conditions: [
        "A consumer installs requests 2.32.0 during a dependency refresh. Because requests 2.32.0 changes the urllib3 constraint to >=2,<3, the installer selects a different allowed urllib3 version.",
      ],
      consumer_scenarios: [
        {
          headline: "Dependency refreshes selecting requests 2.32.0",
          statement:
            "Applications selecting requests 2.32.0 may resolve a different dependency range during installation.",
          verification: "Compare the resolved lockfile before accepting requests 2.32.0.",
          evidence_ids: ["dep-1"],
          conditions: [
            "A consumer installs requests 2.32.0 during a dependency refresh. Because requests 2.32.0 changes the urllib3 constraint to >=2,<3, the installer selects a different allowed urllib3 version.",
          ],
        },
      ],
    },
    customer_impact: {
      valid: true,
      errors: [],
      customer_summary: {
        decision: "publishable_summary",
        impact_type: "dependency_conflict",
        headline: "urllib3 1.x pins block requests 2.32.0",
        affected_if: "You install requests 2.32.0 while pinning urllib3 below 2.",
        what_happens:
          "requests 2.32.0 requires urllib3 >=2,<3, so the installer rejects the incompatible combination.",
        not_affected_if: "Your environment already permits urllib3 2.x.",
        recommended_action: "Update the urllib3 pin or remain on the prior requests release.",
        verification: "Run python -m pip install --dry-run requests==2.32.0 'urllib3<2'.",
        reach_summary: "This package is explicitly monitored.",
        evidence_ids: ["dep-1"],
        limitations: ["Only configured environments were evaluated."],
        decision_card: {
          headline: "urllib3 1.x pins block requests 2.32.0",
          applies_when: "You install requests 2.32.0 while pinning urllib3 below 2.",
          action: "Update the urllib3 pin or remain on the prior requests release.",
          source_scenario_id: "scenario-1",
        },
      },
    },
  },
  model_calls: [],
  source_event: {},
  observability: { analysis_trace_id: "a".repeat(32) },
};
export const insufficientDetail: FindingDetail = {
  ...findingDetail,
  ...insufficientFinding,
  evidence_bundle: {
    collection_status: "complete",
    facts: [{ evidence_id: "requires-python", statement: "Python support changed." }],
    provenance: [{ url: "https://pypi.org/project/cffi/2.1.0/" }],
  },
  limitations: ["The monitored profiles cover Python 3.10 through 3.13, not Python 3.9."],
  gate_results: {
    materiality: findingDetail.gate_results.materiality,
    applicability: {
      assessment: "The configured profiles do not include the affected Python 3.9 scenario.",
      limitations: ["The monitored profiles cover Python 3.10 through 3.13, not Python 3.9."],
    },
    customer_impact: {
      valid: true,
      customer_summary: {
        decision: "insufficient_summary",
        limitations: ["The monitored profiles cover Python 3.10 through 3.13, not Python 3.9."],
      },
    },
  },
};
export const stats: StatsResponse = {
  materiality_counts: { substantive: 1 },
  disposition_counts: { publishable: 1 },
  release_event_count: 1,
  latest_release: {
    event_key: "pypi:requests:2.32.0",
    package_name: "requests",
    version: "2.32.0",
    event_published_at: "2026-07-21T12:00:00Z",
    ingested_at: "2026-07-21T12:00:00Z",
    disposition: "publishable",
    analysis_method: "model_assisted",
    publishable: true,
  },
  unresolved_failures: 0,
  last_ingestion_at: "2026-07-21T12:00:00Z",
  estimated_cost_usd: 0.002,
  price_table_date: "2026-07-20",
};
export const trace: TraceSummary = {
  finding_id: "finding-1",
  analysis_trace_id: "a".repeat(32),
  processing_attempt_id: "attempt-1",
  stage_summary: [
    {
      stage: "connect_source",
      outcome: "completed",
      started_at: null,
      completed_at: null,
      duration_ms: 5,
      detail: "RSS normalized",
    },
    {
      stage: "postgres_persist",
      outcome: "completed",
      started_at: null,
      completed_at: null,
      duration_ms: 12,
      detail: "Committed",
    },
  ],
  model_calls: [],
  grafana_urls: {
    trace: "http://localhost:3001/explore?traceId=aaa",
    logs: "http://localhost:3001/explore?traceId=aaa",
    model_payloads: "http://localhost:3001/d/pypi-reasoning/pypi-reasoning-and-openai",
  },
};
export function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

export function mockFetch(routes: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = requestUrl(input);
    const match = Object.entries(routes).find(([path]) => url.startsWith(path));
    if (!match)
      return new Response(JSON.stringify({ detail: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    return new Response(JSON.stringify(match[1]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}
