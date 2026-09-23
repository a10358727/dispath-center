import { render, screen, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, STUDIO_PATH } from "./App";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => { vi.unstubAllGlobals(); window.location.hash = ""; });

describe("App", () => {
  it("shows the login card (never the shell) when the browser has no session", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(401, { detail: "unauthorized" })));
    render(<App client={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    const link = await screen.findByTestId("login-link");
    expect(link).toHaveAttribute("href", `/auth/login?return_to=${encodeURIComponent(STUDIO_PATH)}`);
    expect(screen.queryByText("核准匣")).not.toBeInTheDocument();
  });

  it("lands on Overview and renders the V0.1 primary and Advanced navigation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) return jsonResponse(200, { authenticated: true, actor: { id: "a1", type: "human", display_name: "operator", platform_admin: true } });
        if (url.includes("/api/v2/approvals")) return jsonResponse(200, { items: [{ id: 7, kind: "enqueue", payload: {}, status: "pending", created_at: "2026-08-30T00:00:00Z" }] });
        if (url.includes("/api/v2/projects-matrix"))
          return jsonResponse(200, {
            projects: [
              // the live matrix keys instances by server name (object, not list)
              { name: "expdemo", repo_or_path: "/srv/expdemo", instances: { "server-a": { path: "/srv/expdemo", git_branch: "main", git_commit: "abcdef1234", dirty: false } } },
            ],
          });
        if (url.includes("/api/v2/jobs")) return jsonResponse(200, []);
        if (url.includes("/api/v2/server-configs")) return jsonResponse(200, []);
        if (url.includes("/api/v2/servers/idle-summary")) return jsonResponse(200, { window_hours: 24, servers: [] });
        if (url.endsWith("/api/v2/servers")) return jsonResponse(200, []);
        if (url.includes("/api/v2/events")) return jsonResponse(200, []);
        return jsonResponse(404, { detail: "not found" });
      }),
    );
    render(<App client={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    expect(await screen.findByRole("heading", { name: /總覽/ })).toBeInTheDocument();
    const nav = within(screen.getByRole("navigation"));
    for (const label of ["總覽", "專案", "運算資源", "活動", "設定", "執行", "資料集", "核准"]) expect(nav.getByText(label)).toBeInTheDocument();
    for (const [label, href] of [["總覽", "/overview"], ["專案", "/projects"], ["運算資源", "/compute"], ["活動", "/activity"], ["設定", "/settings"], ["執行", "/runs"], ["資料集", "/datasets"], ["核准", "/approvals"]]) {
      expect(nav.getByText(label).closest("a")).toHaveAttribute("href", `#${href}`);
    }
    expect(await nav.findByLabelText("1 筆待核准")).toBeInTheDocument();
  });

  it("keeps the legacy Server route as a working alias", async () => {
    window.location.hash = "#/servers?server=server-a";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return jsonResponse(200, { authenticated: true, actor: { id: "a1", type: "human", display_name: "operator", platform_admin: true } });
      if (url.includes("/approvals")) return jsonResponse(200, { items: [] });
      if (url.includes("idle-summary")) return jsonResponse(200, { window_hours: 24, servers: [] });
      if (url.includes("server-configs")) return jsonResponse(200, []);
      if (url.includes("agent-runners")) return jsonResponse(200, { enabled: true, agent_runners: [] });
      if (url.includes("/jobs")) return jsonResponse(200, []);
      if (url.endsWith("/api/v2/servers")) return jsonResponse(200, []);
      return jsonResponse(200, {});
    }));
    render(<App client={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    expect(await screen.findByRole("heading", { name: /運算資源/ })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/compute?server=server-a");
  });

  it("keeps the legacy events route and its filters as an Activity alias", async () => {
    window.location.hash = "#/events?project=demo";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return jsonResponse(200, { authenticated: true, actor: { id: "a1", type: "human", display_name: "operator", platform_admin: true } });
      if (url.includes("/approvals")) return jsonResponse(200, { items: [] });
      if (url.includes("/events")) return jsonResponse(200, []);
      return jsonResponse(200, {});
    }));
    render(<App client={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    expect(await screen.findByRole("heading", { name: /活動/ })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/activity?project=demo");
  });
});
