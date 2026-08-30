import { describe, expect, it } from "vitest";
import { buildTranscript } from "./transcript";
import type { SessionEvent } from "@/api/types";

const at = "2026-08-30T00:00:00+00:00";
const ev = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent => ({ seq, kind, payload: { kind, ...payload }, created_at: at });

describe("buildTranscript", () => {
  it("merges deltas into one live bubble and replaces it with the final text", () => {
    const items = buildTranscript([ev(1, "status", { state: "working" }), ev(2, "text_delta", { text: "hel" }), ev(3, "text_delta", { text: "lo" })]);
    expect(items.at(-1)).toMatchObject({ type: "assistant", text: "hello", streaming: true });
    const finished = buildTranscript([ev(2, "text_delta", { text: "hel" }), ev(3, "text_delta", { text: "lo" }), ev(4, "assistant_text", { text: "hello" })]);
    expect(finished.filter((item) => item.type === "assistant")).toHaveLength(1);
    expect(finished[0]).toMatchObject({ type: "assistant", text: "hello", streaming: false });
  });

  it("folds tool results and permission decisions into the item they answer", () => {
    const items = buildTranscript([
      ev(1, "tool_use", { tool_use_id: "t1", name: "Bash", input: { command: "pytest -q" } }),
      ev(2, "permission", { request_id: "p1", tool_name: "Bash", summary: "pip install rich", allow_pattern: "pip install *", tool_input: { command: "pip install rich" } }),
      ev(3, "permission_decision", { request_id: "p1", decision: "allow", allow_pattern: null }),
      ev(4, "tool_result", { tool_use_id: "t1", is_error: false, content: "1 passed" }),
      ev(5, "result", { is_error: false, total_cost_usd: 0.01, text: "done" }),
    ]);
    expect(items).toHaveLength(3);
    expect(items[0]).toMatchObject({ type: "tool", name: "Bash", result: { isError: false, content: "1 passed" } });
    expect(items[1]).toMatchObject({ type: "permission", requestId: "p1", decision: { decision: "allow" } });
    expect(items[2]).toMatchObject({ type: "result", costUsd: 0.01 });
  });
});
