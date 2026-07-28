import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { App } from "./App";
import { LoadingState } from "./components";
import { NotFound, RouteError } from "./pages/RouteFallback";
import "./styles.css";

const Dashboard = lazy(() =>
  import("./pages/Dashboard").then((module) => ({ default: module.Dashboard })),
);
const Finding = lazy(() =>
  import("./pages/Finding").then((module) => ({ default: module.Finding })),
);

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    errorElement: <RouteError />,
    children: [
      {
        index: true,
        element: (
          <Suspense fallback={<LoadingState />}>
            <Dashboard />
          </Suspense>
        ),
      },
      {
        path: "findings/:findingId",
        element: (
          <Suspense fallback={<LoadingState />}>
            <Finding />
          </Suspense>
        ),
      },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
