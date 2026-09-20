import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OverviewPage } from "./OverviewPage";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function renderOverview() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><OverviewPage /></MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("OverviewPage", () => {
  it("shows intentional loading states while independent projections are pending", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    renderOverview();
    expect(screen.getAllByText("載入中…")).toHaveLength(4);
  });

  it("projects real attention, active work, Compute health, and safe activity links", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("projects-matrix")) return json(200, { projects: [{ id: "p1", name: "demo", instances: { gpu1: { state: "unknown", dirty: false } } }] });
      if (url.includes("approvals")) return json(200, { items: [{ id: 7, project_id: "p1", status: "pending", kind: "execution_plan_v2", created_at: "2026-09-20T00:00:00Z" }] });
      if (url.includes("jobs?status=running")) return json(200, [{ id: 42, status: "running", project: "demo", server: "gpu1" }]);
      if (url.includes("jobs?status=queued")) return json(200, []);
      if (url.includes("server-configs")) return json(200, [{ name: "gpu1", port: 31827, enabled: true }]);
      if (url.includes("idle-summary")) return json(200, { window_hours: 24, servers: [{ server_name: "gpu1", freshness_seconds: 10 }] });
      if (url.endsWith("/api/v2/servers")) return json(200, [{ name: "gpu1", online: true, updated_at: "2026-09-20T00:00:00Z", gpus: [{ mem_total_mb: 24576, util_percent: 4 }] }]);
      if (url.includes("events")) return json(200, [{ event_id: "e1", action: "dispatch", result: "ok", ts: "2026-09-20T00:00:00Z", params: { project: "demo", command: "secret command" } }]);
      return json(404, { detail: "not found" });
    }));

    renderOverview();

    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    for (const heading of ["需要注意的專案", "進行中的 Run", "Compute 健康狀態", "近期活動"]) {
      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
    }
    expect(await screen.findByText(/等待核准 · 卡 #7/)).toBeInTheDocument();
    expect(screen.getByText(/instance 狀態未知/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看 Run" })).toHaveAttribute("href", "/runs?project=demo&job=42&tab=jobs");
    const computeSection = screen.getByRole("heading", { name: "Compute 健康狀態" }).closest("section");
    expect(within(computeSection as HTMLElement).getByText("Connected")).toBeInTheDocument();
    expect(within(computeSection as HTMLElement).getByRole("link", { name: "查看 Compute" })).toHaveAttribute("href", "/compute?server=gpu1");
    expect(screen.getByText(/排入任務|派工/)).toBeInTheDocument();
    expect(screen.queryByText(/secret command/)).not.toBeInTheDocument();
  });

  it("keeps partial data visible and distinguishes blocked access from an empty Compute list", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("server-configs")) return json(403, { detail: "forbidden" });
      if (url.endsWith("/api/v2/servers")) return json(200, [{ name: "observed-only", online: false }]);
      if (url.includes("idle-summary")) return json(200, { window_hours: 24, servers: [{ server_name: "observed-only", freshness_seconds: 5 }] });
      if (url.includes("projects-matrix")) return json(200, { projects: [] });
      if (url.includes("approvals")) return json(200, { items: [] });
      if (url.includes("jobs")) return json(200, []);
      if (url.includes("events")) return json(200, []);
      return json(404, {});
    }));

    renderOverview();

    expect(await screen.findByText("沒有權限查看這部分資料。")).toBeInTheDocument();
    expect(screen.getByText("observed-only")).toBeInTheDocument();
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Add Compute" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重試" })).toBeInTheDocument();
  });
});
