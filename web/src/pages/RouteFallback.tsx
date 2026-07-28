import { CircleAlert, Home, RotateCcw } from "lucide-react";
import { Link, isRouteErrorResponse, useRouteError } from "react-router-dom";
import { AppFrame } from "../App";

export function NotFound() {
  return (
    <RouteFallback
      eyebrow="Page not found"
      title="This route is not available"
      message="The address may be outdated or incomplete. Return to the findings dashboard."
    />
  );
}

export function RouteError() {
  const error = useRouteError();
  const message =
    isRouteErrorResponse(error) && error.status === 404
      ? "The requested page could not be found."
      : "The application could not render this page. Reload it, or return to a known destination.";

  return (
    <AppFrame>
      <RouteFallback
        eyebrow="Page unavailable"
        title="We could not open this page"
        message={message}
        onRetry={() => window.location.reload()}
      />
    </AppFrame>
  );
}

function RouteFallback({
  eyebrow,
  title,
  message,
  onRetry,
}: {
  eyebrow: string;
  title: string;
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="page route-fallback-page">
      <section className="route-fallback-card" aria-labelledby="route-fallback-title">
        <CircleAlert aria-hidden="true" />
        <p className="eyebrow">{eyebrow}</p>
        <h1 id="route-fallback-title">{title}</h1>
        <p>{message}</p>
        <div className="route-fallback-actions">
          {onRetry && (
            <button className="button" type="button" onClick={onRetry}>
              <RotateCcw aria-hidden="true" /> Reload page
            </button>
          )}
          <Link className={onRetry ? "button secondary" : "button"} to="/">
            <Home aria-hidden="true" /> View findings
          </Link>
        </div>
      </section>
    </div>
  );
}
