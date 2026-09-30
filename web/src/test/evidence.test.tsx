import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { Finding } from "../pages/Finding";
import { findingDetail, mockFetch, trace } from "./fixtures";
import type { FindingDetail } from "../types";

afterEach(() => vi.restoreAllMocks());

function show(detail: FindingDetail) {
  mockFetch({
    "/api/findings/finding-1/trace-summary": trace,
    "/api/findings/finding-1": detail,
  });
  render(
    <MemoryRouter initialEntries={["/findings/finding-1"]}>
      <Routes>
        <Route path="/findings/:findingId" element={<Finding />} />
      </Routes>
    </MemoryRouter>,
  );
}

it("preserves recorded null and empty metadata in the complete evidence disclosure", async () => {
  show({
    ...findingDetail,
    evidence_bundle: {
      collection_status: "complete",
      candidate: { metadata: { version: "2.32.0", requires_python: null, requires_dist: [] } },
      computed: { requires_python_diff: { before: ">=3.9", after: null } },
    },
  });
  fireEvent.click(await screen.findByRole("button", { name: "View complete evidence record" }));
  const raw = screen.getByRole("region", { name: "View complete evidence record" });
  expect(within(raw).queryByText("requires python")).toBeInTheDocument();
  expect(within(raw).queryByText("requires dist")).toBeInTheDocument();
});

it("highlights deterministic Python changes citing computed evidence", async () => {
  const evidenceIds = [
    "computed.requires_python_diff.before",
    "computed.requires_python_diff.after",
  ];
  const claims = [{ statement: "Python requirement changed", evidence_ids: evidenceIds }];
  show({
    ...findingDetail,
    analysis_method: "deterministic",
    evidence_bundle: {
      collection_status: "complete",
      computed: { requires_python_diff: { before: ">=3.9", after: ">=3.10" } },
      facts: [
        { evidence_id: evidenceIds[0], value: ">=3.9", source: "computed" },
        { evidence_id: evidenceIds[1], value: ">=3.10", source: "computed" },
      ],
    },
    gate_results: {
      ...findingDetail.gate_results,
      materiality: { decision: "substantive", claims },
      deterministic_impact: { claims, impacts: [] },
      customer_impact: {
        valid: true,
        customer_summary: { headline: "Python 3.9 is excluded", evidence_ids: evidenceIds },
      },
    },
  });
  await screen.findByRole("heading", { name: "Evidence" });
  expect(screen.queryByRole("heading", { name: "Python requirement" })).toBeInTheDocument();
});
