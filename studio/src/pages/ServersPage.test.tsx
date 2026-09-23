import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ServersPage } from "./ServersPage";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function stubEmptyCompute() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("agent-runners")) return json(200, { enabled: true, runners: [], agent_runners: [] });
    if (url.includes("idle-summary")) return json(200, { window_hours: 24, servers: [] });
    if (url.includes("occupancy")) return json(200, { counts: {} });
    if (url.includes("server-configs")) return json(200, []);
    if (url.endsWith("/api/v2/servers")) return json(200, []);
    if (url.includes("/jobs")) return json(200, []);
    return json(200, {});
  }));
}

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><ServersPage /></MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("ServersPage add-compute entry", () => {
  it("opens the guided wizard directly from the header without the advanced section", async () => {
    stubEmptyCompute();
    renderPage();
    expect(await screen.findByText("尚未加入運算資源。")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "新增運算資源" })).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "＋ 新增運算資源" })[0]);

    const wizard = screen.getByRole("region", { name: "新增運算資源" });
    expect(wizard).toBeInTheDocument();
    expect(screen.getByText("第 1 步，共 4 步")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /租用 GPU/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("heading", { name: /進階運算資源設定/ })).not.toBeInTheDocument();
  });

  it("also opens the wizard from the empty-state button", async () => {
    stubEmptyCompute();
    renderPage();
    await screen.findByText("尚未加入運算資源。");
    const buttons = screen.getAllByRole("button", { name: "＋ 新增運算資源" });
    fireEvent.click(buttons[buttons.length - 1]);
    expect(screen.getByRole("region", { name: "新增運算資源" })).toBeInTheDocument();
  });
});
