import type { ReactNode } from "react";
import { AlertTriangle, ArrowLeft, Database, LoaderCircle, RotateCcw } from "lucide-react";
import { Link } from "react-router-dom";

export function LoadingState({ label = "Loading data" }: { label?: string }) {
  return (
    <div className="state-panel" role="status" aria-live="polite">
      <LoaderCircle className="spin" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="state-panel error" role="alert">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>Data unavailable</strong>
        <p>{error.message}</p>
        <div className="state-actions">
          {onRetry && (
            <button className="button" type="button" onClick={onRetry}>
              <RotateCcw aria-hidden="true" /> Retry
            </button>
          )}
          <Link className="button secondary" to="/">
            <ArrowLeft aria-hidden="true" /> Return to findings
          </Link>
        </div>
      </div>
    </div>
  );
}

export function EmptyState({
  children = "No findings match these filters.",
  action,
}: {
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="state-panel" role="status" aria-live="polite">
      <Database aria-hidden="true" />
      <div>
        <p>{children}</p>
        {action && <div className="state-actions">{action}</div>}
      </div>
    </div>
  );
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: string }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function formatPercent(value: number | null | undefined) {
  return value == null ? "—" : `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}%`;
}

export function formatDate(value: string | null | undefined) {
  if (!value) return "Not recorded";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

export function JsonFacts({ value }: { value: unknown }) {
  if (value == null) return <p className="muted">Not recorded.</p>;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return <p>{String(value)}</p>;
  }
  if (Array.isArray(value)) {
    return value.length ? (
      <ul className="fact-list">
        {value.map((item: unknown, i) => (
          <li key={i}>
            <JsonFacts value={item} />
          </li>
        ))}
      </ul>
    ) : (
      <p className="muted">None recorded.</p>
    );
  }
  if (!Object.keys(value).length) return <p className="muted">None recorded.</p>;
  return (
    <div className="json-object">
      {Object.entries(value as Record<string, unknown>).map(([key, item]) => {
        const label = key.replaceAll("_", " ");
        if (item !== null && typeof item === "object") {
          const count = Array.isArray(item) ? item.length : Object.keys(item).length;
          return (
            <details className="fact-group" key={key}>
              <summary>
                <span>{label}</span>
                <small>
                  {count} {Array.isArray(item) ? "items" : "fields"}
                </small>
              </summary>
              <div className="fact-group-content">
                <JsonFacts value={item} />
              </div>
            </details>
          );
        }
        return (
          <dl className="facts" key={key}>
            <div>
              <dt>{label}</dt>
              <dd>
                <JsonFacts value={item} />
              </dd>
            </div>
          </dl>
        );
      })}
    </div>
  );
}
