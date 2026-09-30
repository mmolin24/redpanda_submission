import { useId, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  ChevronDown,
  ExternalLink,
  FileSearch,
  RotateCcw,
  Timer,
} from "lucide-react";
import { Link, useLocation, useParams } from "react-router-dom";
import { api } from "../api";
import { MarkdownText } from "../MarkdownText";
import {
  Badge,
  ErrorState,
  formatDate,
  formatPercent,
  JsonFacts,
  LoadingState,
} from "../components";
import { useAsync, type AsyncState } from "../hooks";
import { formatChangeType } from "../presentation";
import type { FindingDetail, JsonObject, TraceSummary } from "../types";

type UnknownRecord = Record<string, unknown>;

export function Finding() {
  const { findingId = "" } = useParams();
  const location = useLocation();
  const dashboardSearch = asRecord(location.state)?.dashboardSearch;
  const returnTo =
    typeof dashboardSearch === "string" && dashboardSearch.startsWith("?")
      ? `/${dashboardSearch}`
      : "/";
  const detail = useAsync((signal) => api.finding(findingId, signal), [findingId]);
  const trace = useAsync((signal) => api.traceSummary(findingId, signal), [findingId]);

  if (detail.status === "loading")
    return (
      <div className="page">
        <LoadingState label="Loading finding" />
      </div>
    );
  if (detail.status === "error")
    return (
      <div className="page">
        <ErrorState error={detail.error} onRetry={detail.retry} />
      </div>
    );
  const finding = detail.data;
  const changeAnalysis = changeAnalysisRecord(finding.gate_results);
  const applicability = asRecord(finding.gate_results.applicability);
  const customerImpact = asRecord(finding.gate_results.customer_impact);
  const customerSummary = asRecord(customerImpact?.customer_summary);
  const decisionCard = asRecord(customerSummary?.decision_card);
  const deterministicImpact = asRecord(finding.gate_results.deterministic_impact);
  const deterministicImpacts = recordItems(deterministicImpact?.impacts);
  const supportExpansions = recordItems(deterministicImpact?.support_expansions);
  const claims = recordItems(changeAnalysis?.claims);
  const consumerScenarios = recordItems(applicability?.consumer_scenarios);
  const impactConditions = stringItems(applicability?.impact_conditions);
  const summaryLimitations = stringItems(customerSummary?.limitations);
  const limitations = (
    summaryLimitations.length ? summaryLimitations : stringItems(finding.limitations)
  ).filter((item) => !isLegacyCustomerContext(item));
  const evidence = finding.evidence_bundle;
  const computedEvidence = presentEvidence(evidence.computed);
  const factCount = arrayLength(evidence.facts);
  const sourceCount = arrayLength(evidence.provenance);
  const collectionStatus =
    textValue(evidence.collection_status) ?? (finding.evidence_partial ? "partial" : undefined);
  const primaryScenario = consumerScenarios[0];
  const primaryClaim = claims[0];
  const primaryScenarioCondition =
    textValue(primaryScenario?.consumer_trigger) ??
    consumerScenarios.flatMap((scenario) => stringItems(scenario.conditions))[0];
  const primaryCondition =
    textValue(customerSummary?.affected_if) ??
    primaryScenarioCondition ??
    impactConditions[0] ??
    `A consumer selects ${finding.package_name} ${finding.candidate_version}.`;
  const impactHeadline =
    textValue(decisionCard?.headline) ??
    textValue(customerSummary?.headline) ??
    textValue(primaryScenario?.headline) ??
    `Who should review ${finding.package_name} ${finding.candidate_version}`;
  const conciseCondition = textValue(decisionCard?.applies_when) ?? primaryCondition;
  const impactSummary =
    textValue(customerSummary?.what_happens) ??
    textValue(primaryScenario?.statement) ??
    textValue(primaryClaim?.statement) ??
    "Review the supported release change and applicable conditions.";
  const verification =
    textValue(customerSummary?.verification) ??
    textValue(primaryScenario?.verification) ??
    `Check whether your environment can select ${finding.package_name} ${finding.candidate_version}.`;
  const recommendedAction = textValue(customerSummary?.recommended_action);
  const conciseAction =
    textValue(decisionCard?.action) ??
    recommendedAction ??
    "Review the supported change before upgrading.";
  const notAffectedIf = textValue(customerSummary?.not_affected_if);
  const isInsufficient =
    finding.disposition === "insufficient_evidence" ||
    textValue(customerSummary?.decision) === "insufficient_summary";

  return (
    <div className="page finding-page">
      <Link className="back-link" to={returnTo}>
        <ArrowLeft aria-hidden="true" /> Back to findings
      </Link>

      <section className="finding-hero">
        <div className="finding-identity">
          <p className="eyebrow">
            {isInsufficient ? "Evidence-limited analysis" : "Finding detail"}
          </p>
          <h1>
            {finding.package_name} <span>{finding.candidate_version}</span>
          </h1>
          <p className="version-change-large">
            <span>{finding.baseline_version ?? "unknown baseline"}</span>
            <strong>→</strong>
            <span>{finding.candidate_version}</span>
          </p>
          <div className="badge-row finding-badges">
            <Badge tone={finding.disposition}>{humanize(finding.disposition)}</Badge>
            <Badge>
              {finding.analysis_method === "deterministic"
                ? "Deterministic analysis"
                : "Model-assisted analysis"}
            </Badge>
            {finding.processing_priority && (
              <Badge tone={finding.processing_priority}>
                {humanize(finding.processing_priority)} priority
              </Badge>
            )}
            {finding.change_types.map((type) => (
              <Badge key={type}>{formatChangeType(type)}</Badge>
            ))}
            {finding.evidence_partial && <Badge tone="warning">Partial coverage</Badge>}
          </div>
        </div>
        <div className="detail-kpis" aria-label="Finding metrics">
          <Metric label="Release event" value={formatDate(finding.event_published_at)} />
          <Metric label="Analysis confidence" value={formatPercent(finding.confidence)} />
          <Metric label="Analysis completed" value={formatDate(finding.published_at)} />
        </div>
      </section>

      {isInsufficient ? (
        <section
          className="finding-summary insufficient-summary"
          aria-labelledby="finding-summary-title"
        >
          <div className="summary-heading">
            <div>
              <p className="eyebrow">Insufficient evidence</p>
              <h2 id="finding-summary-title">Customer impact could not be concluded</h2>
            </div>
            <FileSearch aria-hidden="true" />
          </div>
          <p className="insufficient-explanation">
            The release was analyzed and retained for review, but the available evidence did not
            support a customer-impact statement. No upgrade action is being recommended.
          </p>
          <div className="insufficient-reasons">
            <h3>What limited the conclusion</h3>
            <TextList
              items={limitations}
              empty={
                finding.assessment ??
                "The analysis did not record enough evidence to identify an affected scenario."
              }
            />
          </div>
        </section>
      ) : (
        <section className="finding-summary" aria-labelledby="finding-summary-title">
          <div className="summary-heading">
            <div>
              <p className="eyebrow">Consumer impact</p>
              <h2 id="finding-summary-title">
                <MarkdownText inline>{impactHeadline}</MarkdownText>
              </h2>
            </div>
            <CheckCircle2 aria-hidden="true" />
          </div>
          <div className="summary-grid">
            <SummaryItem label="Applies when" value={conciseCondition} />
            <SummaryItem label="Do this" value={conciseAction} />
          </div>
          <Disclosure label="Why this applies and how to verify">
            <div className="decision-details">
              <section>
                <h3>What happens</h3>
                <MarkdownText>{impactSummary}</MarkdownText>
              </section>
              <section>
                <h3>Verify with</h3>
                <MarkdownText>{verification}</MarkdownText>
              </section>
              {notAffectedIf && (
                <section>
                  <h3>Not affected when</h3>
                  <MarkdownText>{notAffectedIf}</MarkdownText>
                </section>
              )}
              {deterministicImpacts.length > 1 && (
                <section>
                  <h3>Other affected environments</h3>
                  <TextList
                    items={deterministicImpacts.slice(1).map(deterministicImpactText)}
                    empty=""
                  />
                </section>
              )}
              {supportExpansions.length > 0 && (
                <section>
                  <h3>Support added</h3>
                  <TextList
                    items={supportExpansions
                      .map((item) => textValue(item.summary))
                      .filter(isPresent)}
                    empty=""
                  />
                </section>
              )}
            </div>
          </Disclosure>
        </section>
      )}

      <div className="detail-grid">
        <div className="detail-main">
          <DetailSection
            number="01"
            title="What changed"
            description="Evidence-supported claims tied back to collected release facts."
          >
            {claims.length > 0 ? (
              <ClaimList claims={claims} />
            ) : (
              <p className="muted">No structured claims were recorded.</p>
            )}
            <Disclosure label="View complete change analysis">
              {changeAnalysis ? (
                <JsonFacts value={changeAnalysis} />
              ) : (
                <p className="muted">No structured change analysis was recorded.</p>
              )}
            </Disclosure>
          </DetailSection>

          <DetailSection
            number="02"
            title="Evidence"
            description="Collected facts and deterministic differences used by the analysis."
          >
            <p className="evidence-comparison">
              {finding.baseline_version
                ? `${finding.package_name} ${finding.baseline_version} → ${finding.candidate_version}`
                : `${finding.package_name} ${finding.candidate_version}`}
            </p>
            <EvidenceHighlights evidence={evidence} claims={claims} summary={customerSummary} />
            {computedEvidence !== undefined && (
              <Disclosure label="View computed differences">
                <JsonFacts value={computedEvidence} />
              </Disclosure>
            )}
            {(collectionStatus || factCount > 0 || sourceCount > 0) && (
              <Disclosure label="View collection details">
                <p>
                  {[
                    collectionStatus && `Collection: ${humanize(collectionStatus)}`,
                    factCount > 0 && `${factCount} facts`,
                    sourceCount > 0 && `${sourceCount} source records`,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
                {collectionStatus && (
                  <p className="muted">
                    Collection status describes the configured collection scope, not exhaustive
                    coverage.
                  </p>
                )}
                {presentEvidence(evidence.provenance) !== undefined && (
                  <JsonFacts value={presentEvidence(evidence.provenance)} />
                )}
              </Disclosure>
            )}
            {Object.keys(evidence).length > 0 && (
              <Disclosure label="View complete evidence record">
                <JsonFacts value={evidence} />
              </Disclosure>
            )}
            <Disclosure label="View source event and model metadata">
              <JsonFacts value={presentEvidence(auditMetadata(finding))} />
            </Disclosure>
          </DetailSection>

          <DetailSection
            number="03"
            title="Limitations"
            description="Known boundaries on how this finding should be interpreted."
          >
            <TextList items={limitations} empty="No limitations were recorded." />
            {!limitations.length && (
              <Disclosure label="View raw limitations record">
                <JsonFacts value={finding.limitations} />
              </Disclosure>
            )}
          </DetailSection>
        </div>

        <TraceCard trace={trace} />
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: React.ReactNode;
}) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail}
    </div>
  );
}

function SummaryItem({ label, value }: { label: string; value: string }) {
  return (
    <article>
      <span>{label}</span>
      <MarkdownText>{value}</MarkdownText>
    </article>
  );
}

function ClaimList({ claims }: { claims: UnknownRecord[] }) {
  return (
    <div className="claim-list">
      {claims.map((claim, index) => {
        const evidenceCount = arrayLength(claim.evidence_ids);
        const conditions = stringItems(claim.conditions);
        const support = textValue(claim.support);
        return (
          <article className="claim-card" key={`${textValue(claim.statement) ?? "claim"}-${index}`}>
            <div className="claim-heading">
              <h3>Claim {index + 1}</h3>
              {support && <Badge>{humanize(support)}</Badge>}
            </div>
            <MarkdownText>
              {textValue(claim.statement) ?? "No claim statement was recorded."}
            </MarkdownText>
            {(conditions.length > 0 || support?.toLowerCase() === "conditional") && (
              <div className="claim-conditions">
                <span>Applies when</span>
                {conditions.length ? (
                  <ul>
                    {conditions.map((condition, conditionIndex) => (
                      <li key={`${condition}-${conditionIndex}`}>
                        <MarkdownText>{condition}</MarkdownText>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>Conditions were not recorded; inspect the complete change analysis.</p>
                )}
              </div>
            )}
            <small>
              {evidenceCount} supporting evidence {evidenceCount === 1 ? "reference" : "references"}
            </small>
          </article>
        );
      })}
    </div>
  );
}

function TextList({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <p className="muted">{empty}</p>;
  return (
    <ul className="readable-list">
      {items.map((item, index) => (
        <li key={`${item}-${index}`}>
          <MarkdownText>{item}</MarkdownText>
        </li>
      ))}
    </ul>
  );
}

// Keep false and zero: they are recorded facts, unlike empty containers.
function presentEvidence(value: unknown): unknown {
  if (value == null || (typeof value === "string" && !value.trim())) return undefined;
  if (Array.isArray(value)) {
    const items = value.map(presentEvidence).filter((item) => item !== undefined);
    return items.length ? items : undefined;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value)
      .map(([key, item]) => [key, presentEvidence(item)] as const)
      .filter(([, item]) => item !== undefined);
    return entries.length ? Object.fromEntries(entries) : undefined;
  }
  return value;
}

function EvidenceHighlights({
  evidence,
  claims,
  summary,
}: {
  evidence: JsonObject;
  claims: UnknownRecord[];
  summary: UnknownRecord | undefined;
}) {
  const references = new Set([
    ...claims.flatMap((claim) => stringItems(claim.evidence_ids)),
    ...stringItems(summary?.evidence_ids),
  ]);
  const facts = recordItems(evidence.facts);
  const byId = new Map(facts.map((fact) => [textValue(fact.evidence_id), fact.value]));
  const fields = new Set(
    facts
      .filter((fact) => references.has(String(fact.evidence_id)))
      .map((fact) => String(fact.evidence_id))
      .filter((id) => /^(baseline|candidate)\./.test(id))
      .map((id) => id.replace(/^(baseline|candidate)\./, "")),
  );
  const hunks = recordItems(asRecord(evidence.context)?.code_hunks).filter(
    (hunk) => references.has(String(hunk.evidence_id)) && textValue(hunk.diff),
  );
  const labels: Record<string, string> = {
    requires_dist: "Dependency requirement",
    requires_python: "Python requirement",
    yanked: "Release availability",
  };
  const computedFields = new Set(
    Array.from(references)
      .map((id) => /^computed\.(.+)\.(?:before|after)(?:\.\d+)?$/.exec(id)?.[1])
      .filter(isPresent),
  );
  const computedLabels: Record<string, string> = {
    requires_python_diff: "Python requirement",
    requires_dist_diff: "Dependency requirement",
    yank_diff: "Release availability",
  };
  const comparisons = [
    ...Array.from(fields, (field) => ({
      key: field,
      label:
        labels[field.split(".")[1]] ?? humanize(field.replace(/\.\d+$/, "").replaceAll(".", " ")),
      before: byId.get(`baseline.${field}`),
      after: byId.get(`candidate.${field}`),
    })),
    ...Array.from(computedFields, (field) => {
      const diff = asRecord(
        field.split(".").reduce<unknown>((value, key) => asRecord(value)?.[key], evidence.computed),
      );
      return {
        key: `computed.${field}`,
        label: computedLabels[field.split(".")[0]] ?? humanize(field.replaceAll(".", " ")),
        before: diff?.before,
        after: diff?.after,
      };
    }),
  ];
  return (
    <div className="evidence-summary">
      {comparisons.map(({ key, label, before, after }) => {
        const baseline = presentEvidence(before);
        const candidate = presentEvidence(after);
        if (baseline === undefined && candidate === undefined) return null;
        const display = (value: unknown) =>
          typeof value === "string" ? value : JSON.stringify(value, null, 2);
        return (
          <article className="evidence-highlights" key={key}>
            <h3>{label}</h3>
            <dl className="evidence-values">
              {baseline !== undefined && (
                <div>
                  <dt>Before</dt>
                  <dd>
                    <code>{display(baseline)}</code>
                  </dd>
                </div>
              )}
              {candidate !== undefined && (
                <div>
                  <dt>After</dt>
                  <dd>
                    <code>{display(candidate)}</code>
                  </dd>
                </div>
              )}
            </dl>
          </article>
        );
      })}
      {hunks.map((hunk) => {
        const changedLines = String(hunk.diff)
          .split("\n")
          .filter((line) => /^[+-](?![+-])/.test(line));
        const additions = changedLines.filter((line) => line.startsWith("+")).length;
        const removals = changedLines.length - additions;
        const compact = changedLines.length <= 8;
        return (
          <article className="evidence-highlights" key={String(hunk.evidence_id)}>
            <h3>
              Supporting archive diff · <code>{textValue(hunk.path) ?? "Cited source"}</code>
            </h3>
            <p className="muted">
              {additions} added lines · {removals} removed lines
            </p>
            {compact && <pre className="evidence-diff">{changedLines.join("\n")}</pre>}
            <Disclosure label={compact ? "View diff with context" : "View full diff with context"}>
              <pre className="evidence-diff evidence-diff-expanded">{String(hunk.diff)}</pre>
            </Disclosure>
          </article>
        );
      })}
    </div>
  );
}

function DetailSection({
  number,
  title,
  description,
  children,
}: {
  number: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="detail-section">
      <header className="detail-section-heading">
        <span className="section-number">{number}</span>
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </header>
      <div className="detail-section-body">{children}</div>
    </section>
  );
}

function Disclosure({ label, children }: { label: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const disclosureId = useId();
  const buttonId = `${disclosureId}-button`;
  const contentId = `${disclosureId}-content`;
  return (
    <div className="raw-disclosure">
      <button
        id={buttonId}
        type="button"
        className="disclosure-button"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((current) => !current)}
      >
        <span>
          <FileSearch aria-hidden="true" /> {label}
        </span>
        <ChevronDown aria-hidden="true" />
      </button>
      {open && (
        <div
          id={contentId}
          className="raw-content"
          role="region"
          aria-labelledby={buttonId}
          tabIndex={0}
        >
          {children}
        </div>
      )}
    </div>
  );
}

function TraceCard({ trace }: { trace: AsyncState<TraceSummary> }) {
  return (
    <aside className="trace-card" aria-labelledby="movement-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Data movement</p>
          <h2 id="movement-title">Analysis trace</h2>
        </div>
        <Timer aria-hidden="true" />
      </div>
      {trace.status === "loading" && <LoadingState label="Loading movement history" />}
      {trace.status === "error" && (
        <div className="inline-degraded" role="alert">
          <p>Persisted movement history is unavailable.</p>
          <button className="button secondary" type="button" onClick={trace.retry}>
            <RotateCcw aria-hidden="true" /> Retry movement history
          </button>
        </div>
      )}
      {trace.status === "success" && (
        <>
          <div className="trace-overview">
            <div>
              <strong>{trace.data.stage_summary.length}</strong>
              <span>stages recorded</span>
            </div>
            <div>
              <strong>{trace.data.model_calls.length}</strong>
              <span>model calls</span>
            </div>
          </div>
          <div className="trace-links">
            {Object.entries(trace.data.grafana_urls).map(([label, url]) => (
              <a
                key={label}
                className="button secondary"
                href={url}
                target="_blank"
                rel="noreferrer"
              >
                <ExternalLink aria-hidden="true" /> {humanize(label)}
              </a>
            ))}
          </div>
          <Disclosure label={`View ${trace.data.stage_summary.length} processing stages and IDs`}>
            <dl className="trace-meta">
              <div>
                <dt>Trace</dt>
                <dd title={trace.data.analysis_trace_id}>{trace.data.analysis_trace_id}</dd>
              </div>
              <div>
                <dt>Attempt</dt>
                <dd>{trace.data.processing_attempt_id}</dd>
              </div>
            </dl>
            {trace.data.stage_summary.length ? (
              <ol className="timeline">
                {trace.data.stage_summary.map((stage, index) => (
                  <li key={`${stage.stage}-${index}`} className={stage.outcome}>
                    <span className="timeline-dot" aria-hidden="true" />
                    <div>
                      <strong>{humanize(stage.stage)}</strong>
                      <MarkdownText>{stage.detail ?? stage.outcome}</MarkdownText>
                      <small>
                        {stage.duration_ms == null
                          ? formatDate(stage.completed_at)
                          : `${stage.duration_ms.toFixed(0)} ms`}
                      </small>
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="muted">No stage timings were persisted.</p>
            )}
          </Disclosure>
        </>
      )}
    </aside>
  );
}

function auditMetadata(finding: FindingDetail): JsonObject {
  return {
    source_event: finding.source_event,
    model_calls: finding.model_calls,
    observability: finding.observability,
  };
}

function asRecord(value: unknown): UnknownRecord | undefined {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : undefined;
}

function changeAnalysisRecord(gateResults: JsonObject): UnknownRecord | undefined {
  if (Object.hasOwn(gateResults, "materiality")) {
    return asRecord(gateResults.materiality);
  }
  return Object.keys(gateResults).length > 0 ? gateResults : undefined;
}

function recordItems(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.map(asRecord).filter(isPresent) : [];
}

function deterministicImpactText(impact: UnknownRecord): string {
  const affectedIf = textValue(impact.affected_if);
  const whatHappens = textValue(impact.what_happens);
  return [affectedIf, whatHappens].filter(isPresent).join(" ");
}

function isLegacyCustomerContext(value: string): boolean {
  return /\bcustomer\b|\borganization-specific\b/i.test(value);
}

function stringItems(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(textValue).filter(isPresent);
  const item = textValue(value);
  return item ? [item] : [];
}

function arrayLength(value: unknown) {
  return Array.isArray(value) ? value.length : 0;
}

function textValue(value: unknown) {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function humanize(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isPresent<T>(value: T | undefined): value is T {
  return value !== undefined;
}
