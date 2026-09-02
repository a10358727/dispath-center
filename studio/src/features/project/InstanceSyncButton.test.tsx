import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InstanceSyncButton } from "./InstanceSyncButton";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("InstanceSyncButton (整頓 U5)", () => {
  it("previews the checkout change, then pins the request to the preview digest", async () => {
    const bodies: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (init?.body) bodies.push(JSON.parse(String(init.body)));
        if (url.endsWith("/instance-update-previews")) return jsonResponse(200, { preview_digest: "d".repeat(64), before: { git_commit: "1111111111" }, desired: { git_commit: "2222222222" } });
        if (url.endsWith("/instance-update-requests")) return jsonResponse(202, { approval_id: 5, status: "pending", preview_digest: "d".repeat(64), replayed: false });
        if (url.endsWith("/api/v2/approvals/5")) return jsonResponse(200, { id: 5, kind: "project_instance_update_v2", title: "同步執行機器到版本", summary: "server-a → 2222222", status: "pending", created_at: "2026-09-02T00:00:00Z", payload: {} });
        return jsonResponse(404, { detail: "not found" });
      }),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <InstanceSyncButton projectId="p1" projectVersionId="v2" instanceId="i1" serverName="server-a" />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "同步到 server-a" }));
    expect(await screen.findByText(/1111111 → 2222222/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "建立同步卡" }));
    expect(await screen.findByText("同步執行機器到版本")).toBeInTheDocument();
    expect(bodies[1]).toEqual({ project_version_id: "v2", instance_id: "i1", expected_preview_digest: "d".repeat(64) });
  });
});
