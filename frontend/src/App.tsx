import { Suspense, lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/layout/AppShell";
import { LiveSystemProvider } from "./hooks/useLiveSystem";
import { ThemeProvider } from "./hooks/useTheme";

import Dashboard from "./pages/Dashboard";
import Documents from "./pages/Documents";

const AuthCallback = lazy(() => import("./pages/AuthCallback"));

const DocumentDetail = lazy(() => import("./pages/DocumentDetail"));
const Approvals = lazy(() => import("./pages/Approvals"));
const OnCall = lazy(() => import("./pages/OnCall"));
const Onboarding = lazy(() => import("./pages/Onboarding"));
const OptimizationPage = lazy(() => import("./pages/Optimization"));
const Policies = lazy(() => import("./pages/Policies"));
const Experiments = lazy(() => import("./pages/Experiments"));
const Settings = lazy(() => import("./pages/Settings"));
const Guide = lazy(() => import("./pages/Guide"));

function PageFallback() {
  return (
    <div style={{ padding: "48px 4px" }}>
      <div className="skeleton skeleton-row" style={{ width: "38%" }} />
      <div className="skeleton" style={{ height: 180, marginBottom: 14 }} />
      <div className="skeleton" style={{ height: 220 }} />
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <LiveSystemProvider>
      <BrowserRouter>
        <AppShell>
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/auth/callback" element={<AuthCallback />} />
              <Route path="/documents" element={<Documents />} />
              <Route path="/documents/:documentId" element={<DocumentDetail />} />
              <Route path="/approvals" element={<Approvals />} />
              <Route path="/oncall" element={<OnCall />} />
              <Route path="/onboarding" element={<Onboarding />} />
              <Route path="/optimization" element={<OptimizationPage />} />
              <Route path="/policies" element={<Policies />} />
              <Route path="/experiments" element={<Experiments />} />
              <Route path="/guide" element={<Guide />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<Dashboard />} />
            </Routes>
          </Suspense>
        </AppShell>
      </BrowserRouter>
      </LiveSystemProvider>
    </ThemeProvider>
  );
}