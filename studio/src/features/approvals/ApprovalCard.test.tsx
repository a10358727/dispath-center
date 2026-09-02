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

describe("ApprovalCard kind labels", () => {
  // The pilot flow lives on these two kinds; they must never fall back to the
  // raw snake_case identifier (the old map keyed them wrongly as
  // `experiment_v2` / `code_promotion`).
  it("labels experiment_create_v2 in Chinese", () => {
    renderCard({ ...base, id: 1, kind: "experiment_create_v2" } as Approval);
    expect(screen.getByText("建立實驗")).toBeInTheDocument();
    expect(screen.queryByText("experiment_create_v2")).not.toBeInTheDocument();
  });

  it("labels engineering_task_promote in Chinese", () => {
    renderCard({ ...base, id: 2, kind: "engineering_task_promote" } as Approval);
    expect(screen.getByText("晉升為正式版本")).toBeInTheDocument();
    expect(screen.queryByText("engineering_task_promote")).not.toBeInTheDocument();
  });
});
