import { readFileSync } from "node:fs";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "../pages/Dashboard";
import { Finding } from "../pages/Finding";
import { NotFound } from "../pages/RouteFallback";
import { App } from "../App";
import { JsonFacts } from "../components";
import {
  finding,
  findingDetail,
  findingPage,
  insufficientDetail,
  insufficientFinding,
  mockFetch,
  requestUrl,
  stats,
  trace,
} from "./fixtures";

afterEach(() => vi.restoreAllMocks());

function FindingNavigation() {
  const navigate = useNavigate();
  return (
    <>
      <Finding />
      <button
        onClick={() => {
          void navigate(-1);
        }}
      >
        Back to results
      </button>
    </>
  );
}

describe("findings dashboard", () => {
  it("restores filters, view, and sorting after opening a finding and going Back", async () => {
    const fetch = mockFetch({
      "/api/findings/finding-1/trace-summary": trace,
      "/api/findings/finding-1": findingDetail,
      "/api/findings?": findingPage,
      "/api/stats": stats,
      "/api/ops/summary": { status: "healthy", freshness: {} },
    });
    render(
      <MemoryRouter>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/findings/:findingId" element={<FindingNavigation />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findAllByRole("link", { name: "requests" });
    fireEvent.change(screen.getByRole("textbox", { name: "Package" }), {
      target: { value: "req" },
    });
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(expect.stringContaining("package=req"), expect.anything()),
    );
    fireEvent.change(screen.getByRole("combobox", { name: "Change type" }), {
      target: { value: "dependency_contract" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Full table" }));
    fireEvent.click(screen.getByText("Advanced filters"));
    fireEvent.change(screen.getByRole("combobox", { name: "Priority" }), {
      target: { value: "high" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Minimum confidence (%)" }), {
      target: { value: "65" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Release event" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled());
    fireEvent.click(screen.getByRole("link", { name: "Open requests finding" }));
    expect(await screen.findByRole("link", { name: "Back to findings" })).toHaveAttribute(
      "href",
      expect.stringContaining("view=full"),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Back to results" }));
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("link", { name: "Open requests finding" }));
    fireEvent.click(await screen.findByRole("link", { name: "Back to findings" }));
    await screen.findByRole("table");
    expect(screen.getByRole("textbox", { name: "Package" })).toHaveValue("req");
    expect(screen.getByRole("combobox", { name: "Change type" })).toHaveValue(
      "dependency_contract",
    );
    expect(screen.getByRole("combobox", { name: "Priority" })).toHaveValue("high");
    expect(screen.getByRole("spinbutton", { name: "Minimum confidence (%)" })).toHaveValue(65);
    expect(screen.getByRole("button", { name: "Full table" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("columnheader", { name: "Release event" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });

  it("ignores invalid filters in a shared URL before requesting findings", async () => {
    const fetch = mockFetch({
      "/api/findings?": findingPage,
      "/api/stats": stats,
      "/api/ops/summary": { status: "healthy", freshness: {} },
    });
    render(
      <MemoryRouter
        initialEntries={[
          "/?min_confidence=101&processing_priority=urgent&sort_direction=sideways&view=other",
        ]}
      >
        <Dashboard />
      </MemoryRouter>,
    );
    await screen.findAllByRole("link", { name: "requests" });
    const queries = fetch.mock.calls
      .map(([input]) => new URL(requestUrl(input), "http://localhost"))
      .filter((url) => url.pathname === "/api/findings");
    expect(
      queries.every(
        (url) =>
          !url.searchParams.has("min_confidence") && !url.searchParams.has("processing_priority"),
      ),
    ).toBe(true);
    expect(screen.getByRole("button", { name: "Quick scan" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByText("Advanced filters"));
    expect(screen.getByRole("spinbutton", { name: "Minimum confidence (%)" })).toHaveValue(null);
    expect(screen.getByRole("combobox", { name: "Priority" })).toHaveValue("");
  });

  it("renders ranked findings and operational summary", async () => {
    const fetch = mockFetch({
      "/api/findings?": findingPage,
      "/api/stats": stats,
      "/api/ops/summary": {
        status: "healthy",
        freshness: {
          source_mode: "fixture",
          status: "fixture",
          last_ingestion_at: "2026-07-21T12:00:00Z",
          age_seconds: 0,
          detail: "Static fixture data is available.",
        },
        attention: {
          status: "attention",
          unresolved_failures: 4,
          detail:
            "4 failed processing records are safely retained for review; this count does not indicate infrastructure backpressure.",
        },
        components: {},
        recent_dispositions: {},
        unresolved_failures: 4,
        openai: {},
      },
    });
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    expect(await screen.findAllByRole("link", { name: "requests" })).toHaveLength(2);
    expect(screen.getByRole("heading", { name: "Review first" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Release changes" })).toBeInTheDocument();
    expect(screen.getByText("Monitored releases")).toBeInTheDocument();
    expect(screen.getByText("Published changes")).toBeInTheDocument();
    expect(screen.getByText("Most recent event")).toBeInTheDocument();
    expect(screen.getAllByText("86%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Dependency rules changed").length).toBeGreaterThan(0);
    expect(screen.getAllByText(finding.assessment!)).toHaveLength(2);
    expect(screen.getByText("Dashboard data healthy")).toHaveAttribute("role", "status");
    expect(screen.getByText("Fixture data")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Data details"));
    expect(screen.getByText("Static fixture data is available.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Processing records to review")).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "4 failed processing records are safely retained for review; this count does not indicate infrastructure backpressure.",
      ),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Quick scan" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Full table" }));
    expect(screen.getByRole("button", { name: "Full table" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("columnheader", { name: "Release event" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(screen.getByText("PyPI published 2.32.0")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("sort_by=event_time"),
      expect.anything(),
    );
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("include_insufficient=true"),
      expect.anything(),
    );
    expect(screen.getByRole("option", { name: "Medium" })).toHaveValue("medium");
  });

  it("validates confidence percentages before requesting findings", async () => {
    const fetch = mockFetch({
      "/api/findings?": findingPage,
      "/api/stats": stats,
      "/api/ops/summary": { status: "healthy", freshness: {} },
    });
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await screen.findAllByRole("link", { name: "requests" });
    fireEvent.click(screen.getByText("Advanced filters"));
    const input = screen.getByRole("spinbutton", { name: "Minimum confidence (%)" });
    fireEvent.change(input, { target: { value: "65" } });
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("min_confidence=0.65"),
        expect.anything(),
      ),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled());
    fetch.mockClear();
    for (const value of ["101", "-1"]) {
      fireEvent.change(input, { target: { value } });
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByRole("alert")).toHaveTextContent("Enter a percentage from 0 to 100.");
      expect(screen.getAllByRole("link", { name: "requests" })).toHaveLength(2);
      expect(fetch).not.toHaveBeenCalled();
    }
    for (const [value, normalized] of [
      ["100", "1"],
      ["0", "0"],
    ]) {
      fireEvent.change(input, { target: { value } });
      await waitFor(() =>
        expect(fetch).toHaveBeenCalledWith(
          expect.stringContaining(`min_confidence=${normalized}`),
          expect.anything(),
        ),
      );
      expect(input).not.toHaveAttribute("aria-invalid");
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled(),
      );
      fetch.mockClear();
    }
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(input).toHaveValue(null);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(fetch.mock.calls.map(([input]) => requestUrl(input)).join(" ")).not.toContain(
      "min_confidence",
    );
  });

  it("retries an explicit API error in place", async () => {
    let findingsAttempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(requestUrl(input), "http://localhost");
      if (url.pathname === "/api/findings") {
        if (!url.searchParams.has("sort_by")) {
          return new Response(JSON.stringify(findingPage), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        findingsAttempts += 1;
        const body = findingsAttempts === 1 ? { detail: "database unavailable" } : findingPage;
        return new Response(JSON.stringify(body), {
          status: findingsAttempts === 1 ? 503 : 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.pathname === "/api/stats")
        return new Response(JSON.stringify(stats), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/ops/summary")
        return new Response(
          JSON.stringify({
            status: "healthy",
            freshness: {},
            components: {},
            recent_dispositions: {},
            unresolved_failures: 0,
            openai: {},
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      return new Response(JSON.stringify({ detail: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    });
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findAllByRole("link", { name: "requests" })).toHaveLength(2);
    expect(findingsAttempts).toBe(2);
  });

  it("reports an unavailable operational summary as degraded data access", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(requestUrl(input), "http://localhost");
      if (url.pathname === "/api/findings")
        return new Response(JSON.stringify(findingPage), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/stats")
        return new Response(JSON.stringify(stats), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/ops/summary")
        return new Response(JSON.stringify({ detail: "database unavailable" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      return new Response(JSON.stringify({ detail: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Dashboard data degraded")).toHaveAttribute("role", "alert");
    expect(screen.getByText("Data recency unavailable")).toBeInTheDocument();
  });

  it("preserves filter focus and previous results during a pending request and after failure", async () => {
    const fetch = mockFetch({
      "/api/findings?": findingPage,
      "/api/stats": stats,
      "/api/ops/summary": { status: "healthy", freshness: {} },
    });
    const initialFetch = fetch.getMockImplementation()!;
    let finishRequest!: (response: Response) => void;
    fetch.mockImplementation((input, init) => {
      if (requestUrl(input).includes("package=missing")) {
        return new Promise<Response>((resolve) => {
          finishRequest = resolve;
        });
      }
      return initialFetch(input, init);
    });
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await screen.findAllByRole("link", { name: "requests" });
    const input = screen.getByRole("textbox", { name: "Package" });
    expect(input.closest("details")).toBeNull();
    input.focus();
    fireEvent.change(input, { target: { value: "missing" } });
    await screen.findByText("Refreshing findings… Showing previous results.");
    expect(input).toHaveFocus();
    expect(screen.getAllByRole("link", { name: "requests" })).toHaveLength(2);
    await act(async () =>
      finishRequest(new Response(JSON.stringify({ detail: "Unavailable" }), { status: 503 })),
    );
    await screen.findByRole("button", { name: "Retry" });
    expect(screen.getByRole("textbox", { name: "Package" })).toBe(input);
    expect(input).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findAllByRole("link", { name: "requests" })).toHaveLength(2);
  });

  it("refreshes findings and both summaries while preserving the selected view and filters", async () => {
    const fetch = mockFetch({
      "/api/findings?": findingPage,
      "/api/stats": stats,
      "/api/ops/summary": { status: "healthy", freshness: {} },
    });
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await screen.findAllByRole("link", { name: "requests" });
    fireEvent.click(screen.getByRole("button", { name: "Full table" }));
    fireEvent.click(screen.getByText("Advanced filters"));
    fireEvent.change(screen.getByRole("combobox", { name: "Priority" }), {
      target: { value: "high" },
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled());
    fetch.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh data" })).toBeEnabled());
    const urls = fetch.mock.calls.map(([input]) => requestUrl(input));
    expect(urls).toHaveLength(4);
    expect(urls).toContain("/api/stats");
    expect(urls).toContain("/api/ops/summary");
    const findingQueries = urls
      .filter((url) => url.startsWith("/api/findings?"))
      .map((url) => new URL(url, "http://localhost").searchParams);
    expect(findingQueries).toHaveLength(2);
    expect(
      findingQueries.filter((query) => query.get("processing_priority") === "high"),
    ).toHaveLength(1);
    expect(findingQueries.filter((query) => !query.has("processing_priority"))).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Full table" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("combobox", { name: "Priority" })).toHaveValue("high");
  });

  it("keeps filters available and offers one recovery action when no results match", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(requestUrl(input), "http://localhost");
      if (url.pathname === "/api/findings") {
        const body = url.searchParams.get("package")
          ? { items: [], meta: { page: 1, page_size: 100, total: 0, pages: 0 } }
          : findingPage;
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.pathname === "/api/stats")
        return new Response(JSON.stringify(stats), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/ops/summary")
        return new Response(
          JSON.stringify({
            status: "healthy",
            freshness: {},
            components: {},
            recent_dispositions: {},
            unresolved_failures: 0,
            openai: {},
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      return new Response(JSON.stringify({ detail: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    expect(await screen.findAllByRole("link", { name: "requests" })).toHaveLength(2);
    expect(screen.getByRole("textbox", { name: "Package" }).closest("details")).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Package" }), {
      target: { value: "no-such-package" },
    });
    expect(await screen.findByText("No findings match these filters.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Package" })).toHaveValue("no-such-package");
    expect(screen.getAllByRole("button", { name: "Clear filters" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findAllByRole("link", { name: "requests" })).toHaveLength(2);
  });

  it("loads every findings page before deriving the dashboard", async () => {
    const secondFinding = {
      ...finding,
      finding_id: "finding-2",
      package_name: "urllib3",
      baseline_version: "2.2.0",
      candidate_version: "2.3.0",
    };
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(requestUrl(input), "http://localhost");
      if (url.pathname === "/api/findings") {
        const page = Number(url.searchParams.get("page") ?? "1");
        const body =
          page === 1
            ? { items: [finding], meta: { page: 1, page_size: 100, total: 2, pages: 2 } }
            : { items: [secondFinding], meta: { page: 2, page_size: 100, total: 2, pages: 2 } };
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.pathname === "/api/stats")
        return new Response(JSON.stringify(stats), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/ops/summary")
        return new Response(
          JSON.stringify({
            status: "healthy",
            freshness: {},
            components: {},
            recent_dispositions: {},
            unresolved_failures: 0,
            openai: {},
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      return new Response(JSON.stringify({ detail: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    expect(await screen.findAllByRole("link", { name: "urllib3" })).toHaveLength(2);
    expect(
      screen.getByText("2 distinct releases, expressed in plain language."),
    ).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("page=2"), expect.anything());
  });

  it("renders an observed zero release count", async () => {
    mockFetch({
      "/api/findings?": { items: [], meta: { page: 1, page_size: 100, total: 0, pages: 0 } },
      "/api/stats": {
        ...stats,
        disposition_counts: { suppressed_validation_failure: 1 },
        latest_release: {
          event_key: "pypi:packaging:26.2",
          package_name: "packaging",
          version: "26.2",
          event_published_at: "2026-04-24T20:15:23Z",
          ingested_at: "2026-07-21T12:00:00Z",
          disposition: "suppressed_validation_failure",
          analysis_method: "model_assisted",
          publishable: false,
        },
      },
      "/api/ops/summary": {
        status: "healthy",
        freshness: {},
        components: {},
        recent_dispositions: {},
        unresolved_failures: 0,
        openai: {},
      },
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );

    const releaseCard = (await screen.findByText("Published changes")).closest("article");
    if (!releaseCard) {
      throw new Error("Glance values must be grouped with their labels.");
    }
    expect(within(releaseCard).getByText("0", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("packaging", { selector: "strong" })).toBeInTheDocument();
    const releaseDate = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date("2026-04-24T20:15:23Z"));
    expect(screen.getByText(`26.2 · ${releaseDate}`)).toBeInTheDocument();
    expect(screen.queryByLabelText("Latest monitored release status")).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "Suppressed validation failure. No customer-facing finding was published because the analysis did not pass the publication boundary.",
      ),
    ).not.toBeInTheDocument();
  });

  it("publishes insufficient evidence as a labeled analysis without counting it as impact", async () => {
    mockFetch({
      "/api/findings?": {
        items: [finding, insufficientFinding],
        meta: { page: 1, page_size: 100, total: 2, pages: 1 },
      },
      "/api/stats": {
        ...stats,
        latest_release: {
          event_key: "pypi:cffi:2.1.0",
          package_name: "cffi",
          version: "2.1.0",
          event_published_at: "2026-07-22T12:00:00Z",
          ingested_at: "2026-07-22T12:00:01Z",
          disposition: "insufficient_evidence",
          analysis_method: "model_assisted",
          publishable: false,
        },
      },
      "/api/ops/summary": {
        status: "healthy",
        freshness: {},
        components: {},
        recent_dispositions: {},
        unresolved_failures: 0,
        openai: {},
      },
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );

    const evidenceHeading = await screen.findByRole("heading", { name: "Needs evidence" });
    const disclosure = evidenceHeading.closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    expect(
      screen
        .getByRole("heading", { name: "Release changes" })
        .compareDocumentPosition(evidenceHeading) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    fireEvent.click(disclosure!.querySelector("summary")!);
    expect(disclosure).toHaveAttribute("open");
    expect(screen.getByRole("link", { name: "cffi 2.1.0" })).toBeInTheDocument();
    expect(screen.getAllByText("Insufficient evidence").length).toBeGreaterThan(0);
    expect(
      screen.getByText("1 distinct releases, expressed in plain language."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "Published as an evidence-limited analysis; no customer-impact conclusion was produced.",
      ),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Full table" }));
    expect(screen.getByRole("link", { name: "cffi" })).toBeInTheDocument();
    expect(screen.getByText("2 persisted analyses with detailed metadata.")).toBeInTheDocument();
  });
});

it("labels empty evidence objects instead of rendering blank fact rows", () => {
  render(<JsonFacts value={{ dependency_diff: {} }} />);
  expect(screen.getByText("dependency diff")).toBeInTheDocument();
  expect(screen.getByText("None recorded.")).toBeInTheDocument();
});

describe("application shell", () => {
  it("renders a keyboard-visible skip link into the React application", () => {
    render(
      <MemoryRouter>
        <Routes>
          <Route path="/" element={<App />}>
            <Route index element={<p>Content</p>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute(
      "href",
      "#main-content",
    );
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
  });

  it("keeps finding detail inside the Findings navigation section and focuses main content", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<App />}>
            <Route index element={<Link to="/findings/finding-1">Open finding</Link>} />
            <Route path="findings/:findingId" element={<h1>Finding route</h1>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "Open finding" }));
    expect(await screen.findByRole("heading", { name: "Finding route" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Findings" })).toHaveAttribute("aria-current", "page");
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("renders an in-product fallback for unknown routes", () => {
    render(
      <MemoryRouter initialEntries={["/missing"]}>
        <Routes>
          <Route path="/" element={<App />}>
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "This route is not available" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View findings" })).toHaveAttribute("href", "/");
    expect(screen.queryByText(/developer/i)).not.toBeInTheDocument();
  });
});

describe("frontend portability", () => {
  it("loads no third-party assets from the browser entry document or stylesheet", () => {
    const browserEntrySources = [
      readFileSync("index.html", "utf8"),
      readFileSync("src/styles.css", "utf8"),
    ];
    expect(browserEntrySources.join("\n")).not.toMatch(
      /(?:@import\s+(?:url\()?|(?:src|href)\s*=\s*|url\()\s*["']?https?:\/\//i,
    );
  });
});

describe("finding detail", () => {
  it("labels insufficient evidence without inventing an impact or action", async () => {
    mockFetch({
      "/api/findings/finding-cffi/trace-summary": trace,
      "/api/findings/finding-cffi": insufficientDetail,
    });
    render(
      <MemoryRouter initialEntries={["/findings/finding-cffi"]}>
        <Routes>
          <Route path="/findings/:findingId" element={<Finding />} />
        </Routes>
      </MemoryRouter>,
    );

    const heading = await screen.findByRole("heading", {
      name: "Customer impact could not be concluded",
    });
    const summary = heading.closest("section");
    if (!summary) throw new Error("The insufficient outcome must label its summary section.");
    expect(within(summary).getByText(/No upgrade action is being recommended/)).toBeInTheDocument();
    expect(
      within(summary).getByText(
        "The monitored profiles cover Python 3.10 through 3.13, not Python 3.9.",
      ),
    ).toBeInTheDocument();
    expect(within(summary).queryByText("Do this")).not.toBeInTheDocument();
    expect(screen.queryByText(/Who should review cffi/)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Evidence" })).toBeInTheDocument();
  });

  it("presents customer impact before the supporting analysis", async () => {
    mockFetch({
      "/api/findings/finding-1/trace-summary": trace,
      "/api/findings/finding-1": findingDetail,
    });
    render(
      <MemoryRouter initialEntries={["/findings/finding-1"]}>
        <Routes>
          <Route path="/findings/:findingId" element={<Finding />} />
        </Routes>
      </MemoryRouter>,
    );

    const headline = await screen.findByRole("heading", {
      level: 2,
      name: "urllib3 1.x pins block requests 2.32.0",
    });
    const summary = headline.closest("section");
    if (!summary) throw new Error("The customer-impact headline must label its summary section.");
    expect(
      within(summary).getByText("You install requests 2.32.0 while pinning urllib3 below 2."),
    ).toBeInTheDocument();
    expect(
      within(summary).getByText("Update the urllib3 pin or remain on the prior requests release."),
    ).toBeInTheDocument();
    expect(within(summary).queryByText(/pip install --dry-run/)).not.toBeInTheDocument();

    fireEvent.click(
      within(summary).getByRole("button", { name: "Why this applies and how to verify" }),
    );
    expect(
      within(summary).getByText(
        "Run python -m pip install --dry-run requests==2.32.0 'urllib3<2'.",
      ),
    ).toBeInTheDocument();

    const sectionHeadings = screen
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);
    expect(sectionHeadings[0]).toBe("urllib3 1.x pins block requests 2.32.0");
    expect(screen.getAllByText("Dependency rules changed").length).toBeGreaterThan(0);
    expect(screen.queryByRole("heading", { name: "Impact cases" })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("heading", { level: 2, name: /consumer impact/i })).toHaveLength(
      0,
    );
    expect(screen.queryByText(/\bgate\s*[23]\b/i)).not.toBeInTheDocument();
  });

  it("keeps raw evidence hidden until the customer requests it", async () => {
    mockFetch({
      "/api/findings/finding-1/trace-summary": trace,
      "/api/findings/finding-1": findingDetail,
    });
    render(
      <MemoryRouter initialEntries={["/findings/finding-1"]}>
        <Routes>
          <Route path="/findings/:findingId" element={<Finding />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "What changed" })).toBeInTheDocument();
    expect(
      screen.getByText("A consumer resolves the affected dependency range."),
    ).toBeInTheDocument();
    expect(screen.getByText("Release event")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Evidence" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Limitations" })).toBeInTheDocument();
    expect(screen.queryByText("dep-1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "View complete evidence record" }));
    expect(await screen.findByText("dep-1")).toBeInTheDocument();
    const evidenceRegion = screen.getByRole("region", { name: "View complete evidence record" });
    expect(evidenceRegion).toHaveAttribute("tabindex", "0");
  });

  it("labels deterministic findings and reveals secondary resolved impacts", async () => {
    const deterministicDetail = {
      ...findingDetail,
      analysis_method: "deterministic" as const,
      gate_results: {
        ...findingDetail.gate_results,
        deterministic_impact: {
          decision: "impact_detected",
          impacts: [
            {
              affected_if: "You install requests 2.32.0 on Python 3.9.",
              what_happens: "Requires-Python excludes this release.",
            },
            {
              affected_if: "You install on macOS x86-64 before 10.15.",
              what_happens: "Installation may require a source build.",
            },
          ],
          support_expansions: [
            { summary: "Published wheel support now includes Python 3.15 wheels." },
          ],
        },
      },
    };
    mockFetch({
      "/api/findings/finding-1/trace-summary": trace,
      "/api/findings/finding-1": deterministicDetail,
    });
    render(
      <MemoryRouter initialEntries={["/findings/finding-1"]}>
        <Routes>
          <Route path="/findings/:findingId" element={<Finding />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Deterministic analysis")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Why this applies and how to verify" }));
    expect(
      screen.getByRole("heading", { name: "Other affected environments" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/macOS x86-64 before 10.15/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Support added" })).toBeInTheDocument();
    expect(screen.getByText(/Python 3.15 wheels/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Limitations" })).toBeInTheDocument();
    expect(screen.getByText(/configured environments/i)).toBeInTheDocument();
  });

  it("discloses persisted data movement and the trace destination", async () => {
    mockFetch({
      "/api/findings/finding-1/trace-summary": trace,
      "/api/findings/finding-1": findingDetail,
    });
    render(
      <MemoryRouter initialEntries={["/findings/finding-1"]}>
        <Routes>
          <Route path="/findings/:findingId" element={<Finding />} />
        </Routes>
      </MemoryRouter>,
    );

    const traceDisclosure = await screen.findByRole("button", {
      name: "View 2 processing stages and IDs",
    });
    fireEvent.click(traceDisclosure);
    expect(traceDisclosure).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("Connect Source")).toBeInTheDocument();
    expect(screen.getByText("Postgres Persist")).toBeInTheDocument();
    expect(screen.queryByText(/presentation trace/i)).not.toBeInTheDocument();
    for (const name of ["Trace", "Logs", "Model payloads"]) {
      const link = screen.getByRole("link", { name: new RegExp(`^${name}$`, "i") });
      expect(link).toHaveAttribute("href", expect.stringContaining("localhost:3001"));
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noreferrer");
    }
  });

  it("shows a generic fallback when no consumer scenario is published", async () => {
    const noScenarioDetail = {
      ...findingDetail,
      assessment: "No customer inventory is configured.",
      limitations: ["No customer installation is recorded.", "Configured environments only."],
      gate_results: {
        ...findingDetail.gate_results,
        customer_impact: undefined,
        applicability: {
          assessment: "No customer inventory is configured.",
          impact_conditions: [],
          consumer_scenarios: [],
          customer_impacts: [],
          missing_customer_context: ["Customer profile is not configured."],
          limitations: ["No customer installation is recorded.", "Configured environments only."],
        },
      },
    };
    mockFetch({
      "/api/findings/finding-1/trace-summary": trace,
      "/api/findings/finding-1": noScenarioDetail,
    });
    render(
      <MemoryRouter initialEntries={["/findings/finding-1"]}>
        <Routes>
          <Route path="/findings/:findingId" element={<Finding />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Who should review requests 2.32.0" }),
    ).toBeInTheDocument();
    const fallbackSummary = screen.getByRole("region", {
      name: "Who should review requests 2.32.0",
    });
    expect(
      within(fallbackSummary).getByText("A consumer selects requests 2.32.0."),
    ).toBeInTheDocument();
    expect(
      within(fallbackSummary).queryByText("The dependency constraint changed."),
    ).not.toBeInTheDocument();
    fireEvent.click(
      within(fallbackSummary).getByRole("button", {
        name: "Why this applies and how to verify",
      }),
    );
    expect(
      within(fallbackSummary).getByText("The dependency constraint changed."),
    ).toBeInTheDocument();
    expect(screen.getByText("Configured environments only.")).toBeInTheDocument();
    expect(screen.queryByText("Customer profile is not configured.")).not.toBeInTheDocument();
    expect(screen.queryByText(/customer context/i)).not.toBeInTheDocument();
  });

  it("does not expose the numbered result container for a refused analysis", async () => {
    mockFetch({
      "/api/findings/finding-1/trace-summary": trace,
      "/api/findings/finding-1": {
        ...findingDetail,
        disposition: "refused",
        gate_results: { materiality: null },
      },
    });
    render(
      <MemoryRouter initialEntries={["/findings/finding-1"]}>
        <Routes>
          <Route path="/findings/:findingId" element={<Finding />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("No structured claims were recorded.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View complete change analysis" }));
    expect(screen.getByRole("region", { name: "View complete change analysis" })).toHaveTextContent(
      "No structured change analysis was recorded.",
    );
    expect(screen.queryByText(/\bgate\s*[23]\b/i)).not.toBeInTheDocument();
  });

  it("retries degraded trace data without reloading the finding", async () => {
    let traceAttempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = requestUrl(input);
      if (url.includes("/trace-summary")) {
        traceAttempts += 1;
        const body = traceAttempts === 1 ? { detail: "trace store unavailable" } : trace;
        return new Response(JSON.stringify(body), {
          status: traceAttempts === 1 ? 503 : 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.includes("/api/findings/finding-1"))
        return new Response(JSON.stringify(findingDetail), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      return new Response(JSON.stringify({ detail: "not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    });

    render(
      <MemoryRouter initialEntries={["/findings/finding-1"]}>
        <Routes>
          <Route path="/findings/:findingId" element={<Finding />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Persisted movement history is unavailable.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry movement history" }));
    expect(
      await screen.findByRole("button", { name: "View 2 processing stages and IDs" }),
    ).toBeInTheDocument();
    expect(traceAttempts).toBe(2);
  });
});
