import { useState, type Dispatch, type SetStateAction } from "react";
import { useSearchParams } from "react-router-dom";
import type { FindingFilters } from "./api";

const filterKeys = [
  "package",
  "change_type",
  "processing_priority",
  "min_confidence",
  "sort_direction",
] as const;

export function useDashboardState() {
  const [params, setParams] = useSearchParams();
  const confidence = params.get("min_confidence");
  const confidenceThreshold = Number(confidence);
  const validConfidence =
    confidence !== null &&
    confidence.trim() !== "" &&
    Number.isFinite(confidenceThreshold) &&
    confidenceThreshold >= 0 &&
    confidenceThreshold <= 1;
  const priority = params.get("processing_priority");
  const filters: FindingFilters = {
    package: params.get("package") || undefined,
    change_type: params.get("change_type") || undefined,
    processing_priority:
      priority && ["high", "medium", "low"].includes(priority) ? priority : undefined,
    min_confidence: validConfidence ? String(confidenceThreshold) : undefined,
    sort_by: "event_time",
    sort_direction: params.get("sort_direction") === "asc" ? "asc" : "desc",
  };
  const viewMode = params.get("view") === "full" ? "full" : "quick";
  const [confidenceDraft, setConfidenceDraft] = useState<{ source?: string; value: string } | null>(
    null,
  );
  let confidencePercent = "";
  if (confidenceDraft && confidenceDraft.source === filters.min_confidence) {
    confidencePercent = confidenceDraft.value;
  } else if (filters.min_confidence !== undefined) {
    confidencePercent = String(Number((Number(filters.min_confidence) * 100).toFixed(8)));
  }
  const confidenceError =
    confidencePercent !== "" &&
    (!Number.isFinite(Number(confidencePercent)) ||
      Number(confidencePercent) < 0 ||
      Number(confidencePercent) > 100)
      ? "Enter a percentage from 0 to 100."
      : null;

  const setFilters: Dispatch<SetStateAction<FindingFilters>> = (update) => {
    const nextFilters = typeof update === "function" ? update(filters) : update;
    const nextParams = new URLSearchParams(params);
    for (const key of filterKeys) {
      nextParams.delete(key);
      const value = nextFilters[key];
      if (value !== undefined && value !== "" && !(key === "sort_direction" && value === "desc"))
        nextParams.set(key, value);
    }
    setParams(nextParams, { replace: true, preventScrollReset: true });
  };
  const setViewMode = (view: "quick" | "full") => {
    const nextParams = new URLSearchParams(params);
    nextParams.delete("view");
    if (view === "full") nextParams.set("view", view);
    setParams(nextParams, { replace: true, preventScrollReset: true });
  };
  const updateConfidence = (value: string) => {
    const percentage = Number(value);
    if (value !== "" && (!Number.isFinite(percentage) || percentage < 0 || percentage > 100)) {
      setConfidenceDraft({ source: filters.min_confidence, value });
      return;
    }
    const normalized = value === "" ? undefined : String(percentage / 100);
    setConfidenceDraft({ source: normalized, value });
    setFilters({ ...filters, min_confidence: normalized });
  };
  const clearFilters = () => {
    setConfidenceDraft(null);
    setFilters({ sort_by: "event_time", sort_direction: "desc" });
  };
  return {
    filters,
    setFilters,
    viewMode,
    setViewMode,
    confidencePercent,
    confidenceError,
    updateConfidence,
    clearFilters,
  };
}
