import { useMemo } from "react";
import type { ComponentProps, ReactNode } from "react";
import {
  Activity,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  Clock3,
  FileSearch,
  ListFilter,
  RefreshCw,
  Rows3,
  TableProperties,
} from "lucide-react";
import { Link, useLocation } from "react-router-dom";
import { api, type FindingFilters } from "../api";
import {
  Badge,
  EmptyState,
  ErrorState,
  formatDate,
  formatPercent,
  LoadingState,
} from "../components";
import { useAsync, useDebouncedValue } from "../hooks";
import { useDashboardState } from "../useDashboardState";
import { formatChangeType } from "../presentation";
import { MarkdownText } from "../MarkdownText";
import type { FindingPage, FindingSummary } from "../types";

export function Dashboard() {
  const {
    filters,
    setFilters,
    viewMode,
    setViewMode,
    confidencePercent,
    confidenceError,
    updateConfidence,
    clearFilters,
  } = useDashboardState();
  const debouncedPackage = useDebouncedValue(filters.package ?? "", 300);
  const appliedFilters = {
    ...filters,
    package: debouncedPackage,
    include_insufficient: true,
  };
  const queryKey = JSON.stringify(appliedFilters);
  const findings = useAsync((signal) => loadAllFindings(appliedFilters, signal), [queryKey], {
    keepPreviousData: true,
  });
  const changeTypeCatalog = useAsync(
    (signal) => loadAllFindings({ include_insufficient: true }, signal),
    [],
    { keepPreviousData: true },
  );
  const changeTypeOptions = Array.from(
    new Set([
      ...(changeTypeCatalog.data?.items ?? findings.data?.items ?? []).flatMap(
        (item) => item.change_types,
      ),
      ...(filters.change_type ? [filters.change_type] : []),
    ]),
  ).sort((a, b) => formatChangeType(a).localeCompare(formatChangeType(b)));
  const stats = useAsync(api.stats, [], { keepPreviousData: true });
  const ops = useAsync(api.ops, [], { keepPreviousData: true });
  const refreshing = [findings.status, stats.status, ops.status].includes("loading");
  const refresh = () => {
    findings.retry();
    changeTypeCatalog.retry();
    stats.retry();
    ops.retry();
  };

  const items = useMemo(() => findings.data?.items ?? [], [findings.data]);
  const publishableItems = useMemo(() => items.filter((item) => item.publishable), [items]);
  const insufficientItems = useMemo(
    () => items.filter((item) => item.disposition === "insufficient_evidence"),
    [items],
  );
  const releases = useMemo(() => distinctReleases(publishableItems), [publishableItems]);
  const reviewFirst = useMemo(() => newestDistinctPackages(releases).slice(0, 3), [releases]);
  const newestRelease = useMemo(() => newestReleaseEvent(releases), [releases]);
  const completeCount = releases.filter((item) => !item.evidence_partial).length;
  const activeFilterCount = [
    filters.package,
    filters.change_type,
    filters.processing_priority,
    confidencePercent,
  ].filter(Boolean).length;
  const advancedFilterCount = [filters.processing_priority, confidencePercent].filter(
    Boolean,
  ).length;
  const summary = stats.data ?? null;
  const latestObservedRelease = summary?.latest_release ?? null;
  const pipelineStatus = ops.data?.status ?? (ops.status === "error" ? "degraded" : "unknown");
  const freshness = ops.data?.freshness ?? null;
  const releaseCount = findings.data ? releases.length : "—";

  let latestReleaseDetail: string;
  if (latestObservedRelease) {
    latestReleaseDetail = `${latestObservedRelease.version} · ${formatDate(latestObservedRelease.event_published_at)}`;
  } else if (newestRelease) {
    latestReleaseDetail = `${newestRelease.candidate_version} · ${formatDate(newestRelease.event_published_at)}`;
  } else {
    latestReleaseDetail = "No release event recorded";
  }

  let workspaceDescription: string;
  if (!findings.data) {
    workspaceDescription = "Browse monitored releases and their supporting evidence.";
  } else if (viewMode === "quick") {
    workspaceDescription = `${releases.length} distinct releases, expressed in plain language.`;
  } else {
    workspaceDescription = `${findings.data.meta.total} persisted analyses with detailed metadata.`;
  }

  const visibleItemCount = viewMode === "quick" ? releases.length : items.length;
  let findingsContent: ReactNode;
  if (findings.status === "error") {
    findingsContent = <ErrorState error={findings.error} onRetry={findings.retry} />;
  } else if (!findings.data) {
    findingsContent = <LoadingState label="Loading findings" />;
  } else if (visibleItemCount === 0) {
    findingsContent = (
      <EmptyState
        action={
          <button className="button" type="button" onClick={clearFilters}>
            Clear filters
          </button>
        }
      />
    );
  } else if (viewMode === "quick") {
    findingsContent = <QuickScan items={releases} />;
  } else {
    findingsContent = <FullTable items={items} filters={filters} setFilters={setFilters} />;
  }

  return (
    <div className="page dashboard-page">
      <section className="page-heading dashboard-heading">
        <div>
          <p className="eyebrow">Monitored package landscape</p>
          <h1>Package changes that deserve a look</h1>
          <p>Review recent releases, then open a finding for its evidence and analysis trace.</p>
          <div className="source-context">
            <strong>
              {freshness ? sourceModeLabel(freshness.source_mode) : "Data recency unavailable"}
            </strong>
            <details className="dashboard-data">
              <summary>
                Data details <ChevronDown className="disclosure-chevron" aria-hidden="true" />
              </summary>
              <p>
                {freshness?.detail ??
                  (ops.status === "loading"
                    ? "Loading source context."
                    : "The operational summary could not be loaded.")}
              </p>
              <dl>
                <div>
                  <dt>Last ingestion</dt>
                  <dd>{formatDate(summary?.last_ingestion_at)}</dd>
                </div>
                <div>
                  <dt>Estimated model cost</dt>
                  <dd>{summary ? `$${summary.estimated_cost_usd.toFixed(4)}` : "—"}</dd>
                </div>
                <div>
                  <dt>Most recent event</dt>
                  <dd>
                    <strong>
                      {latestObservedRelease?.package_name ?? newestRelease?.package_name ?? "—"}
                    </strong>
                    <span>{latestReleaseDetail}</span>
                  </dd>
                </div>
              </dl>
            </details>
          </div>
        </div>
        <div className="dashboard-actions">
          <div
            className={`system-state ${pipelineStatus}`}
            role={pipelineStatus === "degraded" ? "alert" : "status"}
          >
            <span className="status-dot" aria-hidden="true" />
            Dashboard data {pipelineStatus}
          </div>
          <button type="button" className="refresh-button" onClick={refresh} disabled={refreshing}>
            <RefreshCw aria-hidden="true" />
            {refreshing ? "Refreshing…" : "Refresh data"}
          </button>
        </div>
      </section>

      <section className="glance-grid" aria-label="Findings at a glance">
        <GlanceCard
          icon={<Clock3 aria-hidden="true" />}
          label="Monitored releases"
          value={summary?.release_event_count ?? "—"}
          detail="Persisted source events"
        />
        <GlanceCard
          icon={<Activity aria-hidden="true" />}
          label="Published changes"
          value={releaseCount}
          detail="Customer-facing findings"
        />
        <GlanceCard
          icon={<CheckCircle2 aria-hidden="true" />}
          label="Complete evidence"
          value={releases.length ? formatPercent(completeCount / releases.length) : "—"}
          detail="No partial evidence flags"
        />
      </section>

      {findings.data && reviewFirst.length > 0 && (
        <section className="focus-section" aria-labelledby="review-first-title">
          <div className="section-heading-row">
            <div>
              <p className="eyebrow">Recent monitored releases</p>
              <h2 id="review-first-title">Review first</h2>
              <p>Newest distinct packages with publishable customer impact.</p>
            </div>
            <span>{reviewFirst.length} packages</span>
          </div>
          <div className="focus-grid">
            {reviewFirst.map((item, index) => (
              <FocusCard key={item.finding_id} item={item} rank={index + 1} />
            ))}
          </div>
        </section>
      )}

      <section
        className="findings-workspace"
        aria-labelledby="findings-title"
        aria-busy={findings.status === "loading"}
      >
        <div className="workspace-heading">
          <div>
            <p className="eyebrow">Explore the evidence</p>
            <h2 id="findings-title">{viewMode === "quick" ? "Release changes" : "All analyses"}</h2>
            <p>{workspaceDescription}</p>
            <p className="workspace-refresh" role="status" aria-live="polite">
              {findings.status === "loading" && findings.data
                ? "Refreshing findings… Showing previous results."
                : ""}
            </p>
          </div>
          <div className="workspace-controls">
            <div className="view-switch" aria-label="Finding detail level">
              <button
                type="button"
                aria-pressed={viewMode === "quick"}
                onClick={() => setViewMode("quick")}
              >
                <Rows3 aria-hidden="true" /> Quick scan
              </button>
              <button
                type="button"
                aria-pressed={viewMode === "full"}
                onClick={() => setViewMode("full")}
              >
                <TableProperties aria-hidden="true" /> Full table
              </button>
            </div>
          </div>
        </div>

        <div className="primary-filters filter-grid">
          <label>
            Package
            <input
              value={filters.package ?? ""}
              onChange={(event) => setFilters({ ...filters, package: event.target.value })}
              placeholder="Search packages"
            />
          </label>
          <label>
            Change type
            <span className="select-control">
              <select
                value={filters.change_type ?? ""}
                onChange={(event) => setFilters({ ...filters, change_type: event.target.value })}
              >
                <option value="">All change types</option>
                {changeTypeOptions.map((type) => (
                  <option key={type} value={type}>
                    {formatChangeType(type)}
                  </option>
                ))}
              </select>
              <ChevronDown aria-hidden="true" />
            </span>
          </label>
          <div className="filter-reset">
            {(visibleItemCount > 0 || findings.status === "error" || !findings.data) &&
              activeFilterCount > 0 && (
                <button type="button" className="text-button" onClick={clearFilters}>
                  Clear filters
                </button>
              )}
          </div>
        </div>

        <details className="filter-disclosure" open={advancedFilterCount > 0 || undefined}>
          <summary>
            <span>
              <ListFilter aria-hidden="true" /> Advanced filters{" "}
              {advancedFilterCount > 0 && <strong>{advancedFilterCount}</strong>}
            </span>
            <ChevronDown className="disclosure-chevron" aria-hidden="true" />
          </summary>
          <div className="filter-content">
            <div className="filter-grid">
              <label>
                Priority
                <span className="select-control">
                  <select
                    value={filters.processing_priority ?? ""}
                    onChange={(event) =>
                      setFilters({ ...filters, processing_priority: event.target.value })
                    }
                  >
                    <option value="">All priorities</option>
                    <option value="high">High</option>
                    <option value="medium">Medium</option>
                    <option value="low">Low</option>
                  </select>
                  <ChevronDown aria-hidden="true" />
                </span>
              </label>
              <div className="confidence-field">
                <label htmlFor="minimum-confidence">Minimum confidence (%)</label>
                <input
                  id="minimum-confidence"
                  type="number"
                  min="0"
                  max="100"
                  step="any"
                  value={confidencePercent}
                  onChange={(event) => updateConfidence(event.target.value)}
                  placeholder="65"
                  aria-invalid={confidenceError ? true : undefined}
                  aria-describedby="confidence-feedback"
                />
                <p
                  id="confidence-feedback"
                  className={`field-hint${confidenceError ? " field-error" : ""}`}
                  role={confidenceError ? "alert" : undefined}
                >
                  {confidenceError ?? "0–100%; leave blank for any confidence."}
                </p>
              </div>
            </div>
          </div>
        </details>

        {findingsContent}
      </section>

      {findings.data && insufficientItems.length > 0 && (
        <section className="insufficient-section" aria-labelledby="needs-evidence-title">
          <details className="insufficient-disclosure">
            <summary>
              <h2 id="needs-evidence-title">Needs evidence</h2>
              <span>{insufficientItems.length} analyses</span>
              <ChevronDown className="disclosure-chevron" aria-hidden="true" />
            </summary>
            <div className="insufficient-content">
              <p>
                These releases were analyzed, but the available evidence could not support a
                customer-impact conclusion.
              </p>
              <div className="insufficient-list">
                {insufficientItems.slice(0, 3).map((item) => (
                  <article key={item.finding_id} className="insufficient-card">
                    <FileSearch aria-hidden="true" />
                    <div>
                      <h3>
                        <FindingLink to={`/findings/${encodeURIComponent(item.finding_id)}`}>
                          {item.package_name} {item.candidate_version}
                        </FindingLink>
                      </h3>
                      <p>
                        {item.assessment ??
                          "The analysis did not identify a supported impact scenario."}
                      </p>
                      <div className="badge-row">
                        <Badge tone="insufficient">Insufficient evidence</Badge>
                        {item.change_types.slice(0, 2).map((change) => (
                          <Badge key={change}>{formatChangeType(change)}</Badge>
                        ))}
                      </div>
                    </div>
                    <FindingLink
                      className="card-link"
                      to={`/findings/${encodeURIComponent(item.finding_id)}`}
                    >
                      Review evidence <ArrowRight aria-hidden="true" />
                    </FindingLink>
                  </article>
                ))}
              </div>
            </div>
          </details>
        </section>
      )}
    </div>
  );
}

function FindingLink(props: ComponentProps<typeof Link>) {
  const { search } = useLocation();
  return <Link {...props} state={{ dashboardSearch: search }} />;
}

function GlanceCard({
  icon,
  label,
  value,
  detail,
  tone = "neutral",
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  detail: string;
  tone?: string;
}) {
  return (
    <article className={`glance-card glance-${tone}`}>
      <div className="glance-icon">{icon}</div>
      <div className="glance-copy">
        <div>
          <span>{label}</span>
          <small>{detail}</small>
        </div>
        <strong>{value}</strong>
      </div>
    </article>
  );
}

function FocusCard({ item, rank }: { item: FindingSummary; rank: number }) {
  return (
    <article className="focus-card">
      <div className="focus-card-top">
        <div className="focus-card-title">
          <span className="focus-rank">0{rank}</span>
          <h3>
            <FindingLink to={`/findings/${encodeURIComponent(item.finding_id)}`}>
              {item.package_name}
            </FindingLink>
          </h3>
        </div>
        <Badge tone={item.processing_priority ?? "neutral"}>
          {item.processing_priority ?? "monitored"}
        </Badge>
      </div>
      <p className="version-change">
        {item.baseline_version ?? "unknown"} <span>→</span> {item.candidate_version}
      </p>
      <p className="plain-change">
        <MarkdownText inline>
          {item.assessment?.trim() || item.change_types.map(formatChangeType).join(" · ")}
        </MarkdownText>
      </p>
      <div className="focus-meta">
        <span>
          <CheckCircle2 aria-hidden="true" /> <strong>{formatPercent(item.confidence)}</strong>{" "}
          confidence
        </span>
        <time dateTime={item.event_published_at ?? undefined}>
          <Clock3 aria-hidden="true" /> {formatDate(item.event_published_at)}
        </time>
      </div>
      <FindingLink className="card-link" to={`/findings/${encodeURIComponent(item.finding_id)}`}>
        Review evidence <ArrowRight aria-hidden="true" />
      </FindingLink>
    </article>
  );
}

function QuickScan({ items }: { items: FindingSummary[] }) {
  return (
    <div className="quick-list" aria-label="Quick finding scan">
      {items.map((item) => (
        <article className="scan-row" key={item.finding_id}>
          <div className="scan-signal">
            <Badge>
              {item.change_types[0] ? formatChangeType(item.change_types[0]) : "change"}
            </Badge>
          </div>
          <div className="scan-change">
            <h3>
              <FindingLink to={`/findings/${encodeURIComponent(item.finding_id)}`}>
                {item.package_name}
              </FindingLink>
            </h3>
            <p>
              {item.baseline_version ?? "unknown"} → {item.candidate_version}
            </p>
            <small className="scan-assessment">
              <MarkdownText inline>
                {item.assessment?.trim() || item.change_types.map(formatChangeType).join(" · ")}
              </MarkdownText>
            </small>
          </div>
          <div className="scan-stat">
            <strong>{formatPercent(item.confidence)}</strong>
            <span>confidence</span>
          </div>
          <div className="scan-date">
            <time dateTime={item.event_published_at ?? undefined}>
              {formatDate(item.event_published_at)}
            </time>
            <span>Release event</span>
          </div>
          <FindingLink
            className="icon-link"
            aria-label={`Open ${item.package_name} ${item.candidate_version} finding`}
            to={`/findings/${encodeURIComponent(item.finding_id)}`}
          >
            <ArrowRight aria-hidden="true" />
          </FindingLink>
        </article>
      ))}
    </div>
  );
}

function FullTable({
  items,
  filters,
  setFilters,
}: {
  items: FindingSummary[];
  filters: FindingFilters;
  setFilters: React.Dispatch<React.SetStateAction<FindingFilters>>;
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Package</th>
            <th>Change</th>
            <th>Confidence</th>
            <SortableHeader
              label="Release event"
              sortKey="event_time"
              filters={filters}
              setFilters={setFilters}
            />
            <th>
              <span className="sr-only">Open</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.finding_id}>
              <td>
                <FindingLink
                  className="package-link"
                  to={`/findings/${encodeURIComponent(item.finding_id)}`}
                >
                  {item.package_name}
                </FindingLink>
                <small>
                  {item.baseline_version ?? "unknown"} → {item.candidate_version}
                </small>
              </td>
              <td>
                <div className="badge-row">
                  {item.change_types.slice(0, 3).map((change) => (
                    <Badge key={change}>{formatChangeType(change)}</Badge>
                  ))}
                  {item.evidence_partial && <Badge tone="warning">partial evidence</Badge>}
                  {!item.publishable && <Badge tone="insufficient">Insufficient evidence</Badge>}
                </div>
              </td>
              <td>{formatPercent(item.confidence)}</td>
              <td>
                <time dateTime={item.event_published_at ?? undefined}>
                  {formatDate(item.event_published_at)}
                </time>
                <small>PyPI published {item.candidate_version}</small>
                <small>Analyzed {formatDate(item.published_at)}</small>
              </td>
              <td>
                <FindingLink
                  className="icon-link"
                  aria-label={`Open ${item.package_name} finding`}
                  to={`/findings/${encodeURIComponent(item.finding_id)}`}
                >
                  <ArrowRight aria-hidden="true" />
                </FindingLink>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SortableHeader({
  label,
  sortKey,
  filters,
  setFilters,
}: {
  label: string;
  sortKey: "event_time";
  filters: FindingFilters;
  setFilters: React.Dispatch<React.SetStateAction<FindingFilters>>;
}) {
  const active = filters.sort_by === sortKey;
  const direction = active ? (filters.sort_direction ?? "desc") : undefined;
  const nextDirection = active && direction === "desc" ? "asc" : "desc";
  let ariaSort: "ascending" | "descending" | "none";
  let sortIcon: ReactNode = null;

  if (!active) {
    ariaSort = "none";
  } else if (direction === "asc") {
    ariaSort = "ascending";
    sortIcon = <ArrowUp aria-hidden="true" />;
  } else {
    ariaSort = "descending";
    sortIcon = <ArrowDown aria-hidden="true" />;
  }

  return (
    <th aria-sort={ariaSort}>
      <button
        type="button"
        className="sort-button"
        onClick={() => setFilters({ ...filters, sort_by: sortKey, sort_direction: nextDirection })}
      >
        {label}
        {sortIcon}
      </button>
    </th>
  );
}

function distinctReleases(items: FindingSummary[]) {
  const unique = new Map<string, FindingSummary>();
  for (const item of items) {
    const key = `${item.package_name}\u0000${item.baseline_version ?? ""}\u0000${item.candidate_version}`;
    if (!unique.has(key)) unique.set(key, item);
  }
  return [...unique.values()];
}

function sourceModeLabel(sourceMode: "fixture" | "history" | "live"): string {
  switch (sourceMode) {
    case "fixture":
      return "Fixture data";
    case "history":
      return "Historical replay";
    case "live":
      return "Live PyPI feed";
  }
}

function newestDistinctPackages(items: FindingSummary[]) {
  const sorted = [...items].sort(
    (left, right) =>
      Date.parse(right.event_published_at ?? "") - Date.parse(left.event_published_at ?? ""),
  );
  const packages = new Set<string>();
  return sorted.filter((item) => {
    if (packages.has(item.package_name)) return false;
    packages.add(item.package_name);
    return true;
  });
}

function newestReleaseEvent(items: FindingSummary[]) {
  return [...items].sort(
    (left, right) =>
      Date.parse(right.event_published_at ?? "") - Date.parse(left.event_published_at ?? ""),
  )[0];
}

async function loadAllFindings(filters: FindingFilters, signal: AbortSignal): Promise<FindingPage> {
  const firstPage = await api.findings({ ...filters, page: 1, page_size: 100 }, signal);
  if (firstPage.meta.pages <= 1) return firstPage;

  const remainingPages = await Promise.all(
    Array.from({ length: firstPage.meta.pages - 1 }, (_, index) =>
      api.findings({ ...filters, page: index + 2, page_size: 100 }, signal),
    ),
  );
  const items = [firstPage, ...remainingPages].flatMap((page) => page.items);
  return { ...firstPage, items };
}
