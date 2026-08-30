import { render, screen, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, STUDIO_PATH } from "./App";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("App", () => {
  it("shows the login card (never the shell) when the browser has no session", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(401, { detail: "unauthorized" })));
    render(<App client={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    const link = await screen.findByTestId("login-link");
    expect(link).toHaveAttribute("href", `/auth/login?return_to=${encodeURIComponent(STUDIO_PATH)}`);
    expect(screen.queryByText("核准匣")).not.toBeInTheDocument();
  });

  it("renders the five-entry shell for an authenticated person", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/auth/me")) return jsonResponse(200, { authenticated: true, actor: { id: "a1", type: "human", display_name: "operator", platform_admin: true } });
        if (url.includes("/api/v2/approvals")) return jsonResponse(200, { items: [{ id: 7, kind: "enqueue", payload: {}, status: "pending", created_at: "2026-08-30T00:00:00Z" }] });
        if (url.includes("/api/v2/projects-matrix")) return jsonResponse(200, { projects: [{ name: "expdemo", repo_or_path: "/srv/expdemo", instances: [] }] });
        return jsonResponse(404, { detail: "not found" });
      }),
    );
    render(<App client={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    expect(await screen.findByText("expdemo")).toBeInTheDocument();
    const nav = within(screen.getByRole("navigation"));
    for (const label of ["專案", "實驗與 Run", "伺服器與硬體", "核准匣", "設定"]) expect(nav.getByText(label)).toBeInTheDocument();
    expect(await nav.findByText("1")).toBeInTheDocument();
  });
});
