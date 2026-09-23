import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Transcript } from "./Transcript";
import { ReadyToRunCard, runReference } from "./ReadyToRunCard";
import type { TranscriptItem } from "./transcript";

function tool(result: { isError: boolean; content: string } | null): Extract<TranscriptItem, { type: "tool" }> {
  return { type: "tool", seq: 1, toolUseId: "tool-1", name: "mcp__dispatch__request_run", input: { expected_plan_digest: "secret-in-advanced-only" }, result, at: "2026-09-21T00:00:00Z" };
}

function approval(status = "pending") {
  return {
    id: 41, kind: "execution_plan_v2", title: "Run baseline training", summary: "baseline · rental-4090-001", status,
    created_at: "2026-09-21T00:00:00Z", can_decide: true, payload_verified: true, payload_digest: "a".repeat(64),
    payload: { execution_plan_id: "33333333-3333-4333-8333-333333333333", plan_digest: "b".repeat(64) },
    review: {
      execution_plan_id: "33333333-3333-4333-8333-333333333333", plan_digest: "b".repeat(64),
      contract: {
        contract_version: "execution-plan-v2",
        project_version: { project_version_id: "version-in-advanced", git_commit: "1234567890abcdef1234567890abcdef12345678" },
        target: { server_name: "rental-4090-001", server_config_revision_id: "revision-in-advanced" },
        parameter_values: { batch_size: 16, epochs: 3 }, plan_digest: "b".repeat(64),
      },
    },
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function renderCard(item = tool({ isError: false, content: JSON.stringify({ approval_id: 41, approval_status: "pending" }) })) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><ReadyToRunCard item={item} /></MemoryRouter></QueryClientProvider>);
}

afterEach(() => vi.unstubAllGlobals());

describe("ReadyToRunCard", () => {
  it("accepts only a successful correlated tool result with a valid approval id", () => {
    expect(runReference(tool(null))).toEqual({ state: "loading" });
    expect(runReference(tool({ isError: true, content: "denied" }))).toEqual({ state: "tool-error", message: "denied" });
    expect(runReference(tool({ isError: false, content: "not json" }))).toEqual({ state: "malformed" });
    expect(runReference(tool({ isError: false, content: JSON.stringify({ approval_id: 41 }) }))).toEqual({ state: "ready", approvalId: 41 });
  });

  it("loads verified platform detail and runs through the existing v2 decision route", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url === "/api/v2/approvals/41" && !init?.method) return jsonResponse(200, approval());
      if (url === "/api/v2/approvals/41/decisions") return jsonResponse(202, { status: "approved", job_id: 8 });
      return jsonResponse(404, { detail: "not found" });
    }));
    renderCard();
    expect(await screen.findByText("Run baseline training")).toBeInTheDocument();
    expect(screen.getByText("1234567890ab")).toBeInTheDocument();
    expect(screen.getByText("rental-4090-001")).toBeInTheDocument();
    expect(screen.getByText("batch_size=16 · epochs=3")).toBeInTheDocument();
    expect(screen.getByText("提案時已就緒．執行時重新檢查")).toBeInTheDocument();
    expect(screen.queryByText("version-in-advanced")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看執行紀錄" })).toHaveAttribute("href", "/runs");
    fireEvent.click(screen.getByRole("button", { name: "執行" }));
    await waitFor(() => expect(calls.some((call) => call.url === "/api/v2/approvals/41/decisions" && call.method === "POST")).toBe(true));
    expect(calls.find((call) => call.url.endsWith("/decisions"))?.body).toEqual({ decision: "approve" });
  });

  it("distinguishes loading, malformed, unavailable, and terminal states", async () => {
    const loading = renderCard(tool(null));
    expect(screen.getByText("等待受管控的請求…")).toBeInTheDocument();
    loading.unmount();
    const malformed = renderCard(tool({ isError: false, content: "{}" }));
    expect(screen.getByText("工具結果未包含有效的核准參照。")).toBeInTheDocument();
    malformed.unmount();
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(404, { detail: "not found" })));
    const unavailable = renderCard();
    expect(await screen.findByText("核准詳情不可用，或您無權檢視。")).toBeInTheDocument();
    unavailable.unmount();
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, approval("approved"))));
    const approved = renderCard();
    expect(await screen.findByText("approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "執行" })).not.toBeInTheDocument();
    approved.unmount();
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, approval("rejected"))));
    renderCard();
    expect(await screen.findByText("rejected")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "執行" })).not.toBeInTheDocument();
  });

  it("does not create a card from assistant text and preserves the generic tool fallback", () => {
    Element.prototype.scrollIntoView = vi.fn();
    const items: TranscriptItem[] = [
      { type: "assistant", seq: 1, text: "request_run approval_id 41", streaming: false, at: "2026-09-21T00:00:00Z" },
      { ...tool({ isError: false, content: "ok" }), seq: 2, name: "Bash" },
    ];
    render(<MemoryRouter><Transcript items={items} deciding={false} onDecide={vi.fn()} /></MemoryRouter>);
    expect(screen.queryByTestId("ready-to-run-card")).not.toBeInTheDocument();
    expect(screen.getByText("request_run approval_id 41")).toBeInTheDocument();
    expect(screen.getByText("Bash")).toBeInTheDocument();
  });
});
