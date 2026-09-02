import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApprovalCard } from "./ApprovalCard";
import type { Approval } from "@/api/types";

afterEach(() => vi.unstubAllGlobals());

function renderCard(approval: Approval) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ApprovalCard approval={approval} confirmImmediately={approval.can_decide === true && approval.kind === "execution_plan_v2"} />
    </QueryClientProvider>,
  );
}

const base = { payload: {}, status: "pending", created_at: "2026-09-02T00:00:00Z" } as const;

describe("ApprovalCard 確認並執行 (DG-SINGLE-OPERATOR-CONFIRM v1)", () => {
  function jsonResponse(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  it("decides a closed-list kind immediately through the digest-bound v2 route", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push(`${init?.method ?? "GET"} ${url}`);
        if (url.endsWith("/api/v2/approvals/40")) return jsonResponse(200, { id: 40, kind: "execution_plan_v2", status: "pending", created_at: "2026-09-02T00:00:00Z", payload: {}, payload_digest: "e".repeat(64) });
        if (url.endsWith("/api/v2/approvals/40/decisions")) {
          expect(new Headers(init?.headers).get("X-Approval-Payload-Digest")).toBe("e".repeat(64));
          return jsonResponse(202, { approval_id: 40, status: "approved" });
        }
        return jsonResponse(404, { detail: "not found" });
      }),
    );
    renderCard({ ...base, id: 40, kind: "execution_plan_v2", title: "執行一個 Run", can_decide: true } as Approval);
    expect(await screen.findByText("已由你本人立即核准，完整留稽核。")).toBeInTheDocument();
    expect(calls).toContain("POST /api/v2/approvals/40/decisions");
  });

  it("never decides a kind outside the closed list, even when asked", () => {
    const fetchMock = vi.fn(async () => jsonResponse(404, {}));
    vi.stubGlobal("fetch", fetchMock);
    for (const kind of ["engineering_task_promote", "server_delete", "agent_runner_enroll", "node_enroll", "project_role_change", "hardware_action_v2"]) {
      render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <ApprovalCard approval={{ ...base, id: 41, kind, title: kind } as Approval} confirmImmediately />
        </QueryClientProvider>,
      );
    }
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getAllByRole("button", { name: "核准" }).length).toBe(6);
  });
});

describe("ApprovalCard presentation (整頓 U2)", () => {
  it("renders the backend title and summary instead of the raw kind", () => {
    renderCard({
      ...base,
      id: 1,
      kind: "experiment_create_v2",
      title: "建立實驗",
      summary: "demo · 4 個 run",
    } as Approval);
    expect(screen.getByText("建立實驗")).toBeInTheDocument();
    expect(screen.getByText("demo · 4 個 run")).toBeInTheDocument();
    expect(screen.queryByText("experiment_create_v2")).not.toBeInTheDocument();
  });

  it("falls back to the raw kind only when the backend sent no title", () => {
    renderCard({ ...base, id: 2, kind: "engineering_task_promote" } as Approval);
    expect(screen.getByText("engineering_task_promote")).toBeInTheDocument();
  });

  it("renders the note and disables both buttons with a reason when the card is undecidable", () => {
    renderCard({
      ...base,
      id: 3,
      kind: "server_delete",
      title: "刪除機器設定",
      note: "task_id=abc",
      can_decide: false,
      decision_reason: "denied_high_risk_self_decision",
    } as Approval);
    expect(screen.getByText(/備註：task_id=abc/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "核准" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "退回" })).toBeDisabled();
    expect(screen.getByText(/不允許自核/)).toBeInTheDocument();
  });
});
