import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { ApprovalCard } from "./ApprovalCard";
import type { Approval } from "@/api/types";

function renderCard(approval: Approval) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ApprovalCard approval={approval} />
    </QueryClientProvider>,
  );
}

const base = { payload: {}, status: "pending", created_at: "2026-09-02T00:00:00Z" } as const;

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
