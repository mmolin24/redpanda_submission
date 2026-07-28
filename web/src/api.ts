import type { FindingDetail, FindingPage, OpsSummary, StatsResponse, TraceSummary } from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" }, signal });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // The status is sufficient when the response is not JSON.
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export interface FindingFilters {
  package?: string;
  change_type?: string;
  processing_priority?: string;
  min_confidence?: string;
  disposition?: string;
  include_insufficient?: boolean;
  sort_by?: "event_time";
  sort_direction?: "asc" | "desc";
  page?: number;
  page_size?: number;
}

export const api = {
  findings(this: void, filters: FindingFilters, signal?: AbortSignal): Promise<FindingPage> {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value !== undefined && value !== "") params.set(key, String(value));
    });
    return request(`/api/findings?${params}`, signal);
  },
  finding(this: void, id: string, signal?: AbortSignal) {
    return request<FindingDetail>(`/api/findings/${encodeURIComponent(id)}`, signal);
  },
  traceSummary(this: void, id: string, signal?: AbortSignal) {
    return request<TraceSummary>(`/api/findings/${encodeURIComponent(id)}/trace-summary`, signal);
  },
  stats(this: void, signal?: AbortSignal) {
    return request<StatsResponse>("/api/stats", signal);
  },
  ops(this: void, signal?: AbortSignal) {
    return request<OpsSummary>("/api/ops/summary", signal);
  },
};
