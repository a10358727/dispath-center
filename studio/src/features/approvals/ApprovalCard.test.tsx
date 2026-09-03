import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

describe("ApprovalCard hardware known-good flag (DG-HARDWARE-EXECUTION v1 H-6 (a))", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends mark_known_good only when ticked on a hil_test card", async () => {
    const bodies: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v2/approvals/77")) {
          return new Response(JSON.stringify({ id: 77, kind: "hardware_action_v2", status: "pending", created_at: "2026-09-03T00:00:00Z", payload: { action_class: "hil_test", device_id: "esp32-1" }, payload_digest: "d".repeat(64) }), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        if (url.endsWith("/api/v2/approvals/77/decisions")) {
          bodies.push(String(init?.body));
          return new Response(JSON.stringify({ approval_id: 77, status: "approved", job_id: 5 }), { status: 202, headers: { "Content-Type": "application/json" } });
        }
        return new Response("{}", { status: 404 });
      }),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ApprovalCard approval={{ id: 77, kind: "hardware_action_v2", title: "硬體實體動作", status: "pending", created_at: "2026-09-03T00:00:00Z", payload: { action_class: "hil_test", device_id: "esp32-1" } }} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "核准" }));
    await waitFor(() => expect(bodies.length).toBe(1));
    expect(JSON.parse(bodies[0])).toEqual({ decision: "approve", mark_known_good: true });
  });

  it("shows no checkbox on a program card", () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ApprovalCard approval={{ id: 78, kind: "hardware_action_v2", title: "硬體實體動作", status: "pending", created_at: "2026-09-03T00:00:00Z", payload: { action_class: "program", device_id: "esp32-1" } }} />
      </QueryClientProvider>,
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
