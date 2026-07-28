import { Activity, Radar } from "lucide-react";
import { useEffect, type ReactNode } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";

export function App() {
  return (
    <AppFrame>
      <RouteFocus />
      <Outlet />
    </AppFrame>
  );
}

export function AppFrame({ children }: { children: ReactNode }) {
  const location = useLocation();
  const findingsCurrent = location.pathname === "/" || location.pathname.startsWith("/findings/");

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <header className="topbar">
        <Link className="brand" to="/" aria-label="PyPI Change Intelligence home">
          <Radar aria-hidden="true" />
          <span>
            PyPI <strong>Change Intelligence</strong>
          </span>
        </Link>
        <nav aria-label="Primary navigation">
          <Link
            className={findingsCurrent ? "active" : undefined}
            aria-current={findingsCurrent ? "page" : undefined}
            to="/"
          >
            <Activity aria-hidden="true" /> Findings
          </Link>
        </nav>
      </header>
      <main id="main-content" tabIndex={-1}>
        {children}
      </main>
      <footer>Local, evidence-backed analysis · PyPI public data</footer>
    </div>
  );
}

function RouteFocus() {
  const { pathname } = useLocation();

  useEffect(() => {
    document.getElementById("main-content")?.focus({ preventScroll: true });
  }, [pathname]);

  return null;
}
