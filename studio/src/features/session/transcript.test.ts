import { describe, expect, it } from "vitest";
import { buildTranscript, runAnalysisFor } from "./transcript";
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

  it("correlates a request_run result only by its durable tool_use_id", () => {
    const items = buildTranscript([
      ev(1, "tool_use", { tool_use_id: "run-1", name: "mcp__dispatch__request_run", input: { project_version_id: "v1" } }),
      ev(2, "assistant_text", { text: "approval_id 999" }),
      ev(3, "tool_result", { tool_use_id: "other", is_error: false, content: JSON.stringify({ approval_id: 999 }) }),
      ev(4, "tool_result", { tool_use_id: "run-1", is_error: false, content: JSON.stringify({ approval_id: 41 }) }),
    ]);
    const run = items.find((item) => item.type === "tool" && item.toolUseId === "run-1");
    expect(run?.type === "tool" ? run.result?.content : null).toBe(JSON.stringify({ approval_id: 41 }));
    expect(items.some((item) => item.type === "assistant" && item.text === "approval_id 999")).toBe(true);
  });

  it("requires successful structured evidence for the exact Run and a completed turn", () => {
    const planId = "33333333-3333-4333-8333-333333333333";
    const proseOnly = [
      ev(1, "user_text", { text: `[dispatch:analyze-run:${planId}] analyze` }),
      ev(2, "assistant_text", { text: `I used evidence for ${planId}` }),
      ev(3, "result", { is_error: false, text: "done" }),
    ];
    expect(runAnalysisFor(buildTranscript(proseOnly), planId)).toMatchObject({ requested: false, completed: false, evidence: [] });
    const events = [
      ev(1, "user_text", { text: `[dispatch:analyze-run:${planId}] analyze` }),
      ev(2, "tool_use", { tool_use_id: "e1", name: "mcp__dispatch__get_run", input: { plan_id: planId } }),
      ev(3, "tool_result", { tool_use_id: "e1", is_error: false, content: JSON.stringify({ plan_id: "44444444-4444-4444-8444-444444444444", state: "succeeded" }) }),
      ev(4, "assistant_text", { text: `I used evidence for ${planId}` }),
      ev(5, "result", { is_error: false, text: "done" }),
    ];
    expect(runAnalysisFor(buildTranscript(events), planId).completed).toBe(false);
    events[2] = ev(3, "tool_result", { tool_use_id: "e1", is_error: false, content: JSON.stringify({ plan_id: planId, availability: "known", state: "succeeded" }) });
    const analysis = runAnalysisFor(buildTranscript(events), planId);
    expect(analysis).toMatchObject({ completed: true, text: `I used evidence for ${planId}`, evidence: [{ name: "get_run", toolUseId: "e1", availability: "known" }] });
  });

  it("bounds grounded analysis to its completed turn and allows an ungrounded turn to retry", () => {
    const planId = "33333333-3333-4333-8333-333333333333";
    const initial = [
      ev(1, "user_text", { text: `[dispatch:analyze-run:${planId}] analyze` }),
      ev(2, "tool_use", { tool_use_id: "e1", name: "get_run", input: { plan_id: planId } }),
      ev(3, "tool_result", { tool_use_id: "e1", is_error: false, content: JSON.stringify({ plan_id: planId, state: "succeeded" }) }),
      ev(4, "assistant_text", { text: "Original grounded recommendation" }),
      ev(5, "result", { is_error: false, text: "done" }),
    ];
    const laterTurn = [
      ev(6, "user_text", { text: `[dispatch:continue-run:${planId}] continue` }),
      ev(7, "tool_use", { tool_use_id: "later", name: "get_run_metrics", input: { plan_id: planId } }),
      ev(8, "tool_result", { tool_use_id: "later", is_error: false, content: JSON.stringify({ plan_id: planId, availability: "known" }) }),
      ev(9, "assistant_text", { text: "Later recommendation must not replace the first" }),
      ev(10, "result", { is_error: false, text: "done" }),
    ];
    expect(runAnalysisFor(buildTranscript([...initial, ...laterTurn]), planId)).toMatchObject({
      requested: true,
      completed: true,
      text: "Original grounded recommendation",
      evidence: [{ name: "get_run", toolUseId: "e1" }],
    });

    const ungrounded = buildTranscript([
      ev(11, "user_text", { text: `[dispatch:analyze-run:${planId}] retry analysis` }),
      ev(12, "assistant_text", { text: "No structured evidence" }),
      ev(13, "result", { is_error: true, text: "failed" }),
    ]);
    expect(runAnalysisFor(ungrounded, planId)).toMatchObject({ requested: false, completed: false, evidence: [], text: null });
  });
});
