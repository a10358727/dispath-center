import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PromotePanel } from "./PromotePanel";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("PromotePanel (整頓 U3)", () => {
  it("creates the promote card from the bridge task and renders it in place", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push(`${init?.method ?? "GET"} ${url}`);
        if (url.endsWith("/api/v2/engineering-tasks/task-1/promote-requests")) {
          return jsonResponse(200, {
            approval: { id: 42, kind: "engineering_task_promote", title: "晉升為正式版本", summary: "demo · abcdef12", status: "pending", created_at: "2026-09-02T00:00:00Z", payload: {} },
          });
        }
        return jsonResponse(404, { detail: "not found" });
      }),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <PromotePanel taskId="task-1" project="demo" />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "晉升為正式版本" }));
    expect(await screen.findByText("demo · abcdef12")).toBeInTheDocument();
    // The card is pending: the person still has to approve it (P-1).
    expect(screen.getByRole("button", { name: "核准" })).toBeInTheDocument();
    expect(calls).toContain("POST /api/v2/engineering-tasks/task-1/promote-requests");
  });
});
