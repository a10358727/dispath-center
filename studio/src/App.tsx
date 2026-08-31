import { Component, useEffect, useState, type ErrorInfo, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { onUnauthenticated } from "@/api/client";
import { useMe } from "@/api/hooks";
import { Shell } from "@/components/Shell";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalsPage } from "@/pages/ApprovalsPage";
import { SettingsPage } from "@/pages/PlaceholderPages";
import { RunsPage } from "@/pages/RunsPage";
import { ProjectPage } from "@/pages/ProjectPage";
import { ProjectsPage } from "@/pages/ProjectsPage";
import { ServersPage } from "@/pages/ServersPage";
import { EventsPage } from "@/pages/EventsPage";

export const STUDIO_PATH = "/static/studio/";

function LoginCard() {
  const returnTo = encodeURIComponent(STUDIO_PATH);
  return (
    <div className="flex h-screen items-center justify-center">
      <Card className="w-80 text-center">
        <CardTitle>Dispatch Studio</CardTitle>
        <p className="mb-3 text-sm text-slate-600">請先登入。</p>
        <a className="inline-block rounded-md bg-slate-900 px-4 py-2 text-sm text-white" href={`/auth/login?return_to=${returnTo}`} data-testid="login-link">
          使用 OIDC 登入
        </a>
      </Card>
    </div>
  );
}

/** A render error must never leave a blank page: show what broke so the
 *  person can report it (the console keeps the stack). */
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Studio render error", error, info.componentStack);
  }
  render() {
    if (this.state.error) {
      return (
        <div className="m-6 rounded-lg border border-rose-300 bg-rose-50 p-4 text-sm text-rose-900" data-testid="render-error">
          <div className="font-semibold">Studio 畫面出錯了</div>
          <pre className="mt-2 whitespace-pre-wrap text-xs">{String(this.state.error && (this.state.error.stack || this.state.error.message))}</pre>
          <button type="button" className="mt-2 rounded bg-rose-700 px-3 py-1 text-white" onClick={() => window.location.reload()}>
            重新載入
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

function Gate() {
  const me = useMe();
  const [loggedOut, setLoggedOut] = useState(false);
  useEffect(() => onUnauthenticated(() => setLoggedOut(true)), []);
  if (loggedOut || (me.data && !me.data.authenticated)) return <LoginCard />;
  if (me.isLoading) return <div className="p-6 text-sm text-slate-400">載入中…</div>;
  if (me.error) return <LoginCard />;
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/projects" replace />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:name" element={<ProjectPage />} />
        <Route path="/projects/:name/sessions/:sessionId" element={<ProjectPage />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/servers" element={<ServersPage />} />
        <Route path="/approvals" element={<ApprovalsPage />} />
        <Route path="/events" element={<EventsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/projects" replace />} />
      </Route>
    </Routes>
  );
}

export function App({ client }: { client?: QueryClient }) {
  const [queryClient] = useState(() => client ?? new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } }));
  return (
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <ErrorBoundary>
          <Gate />
        </ErrorBoundary>
      </HashRouter>
    </QueryClientProvider>
  );
}
