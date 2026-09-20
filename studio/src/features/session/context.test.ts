import { describe, expect, it } from "vitest";
import type { SessionEvent } from "@/api/types";
import { latestContextUsage } from "./context";

const event = (seq: number, kind: string, payload: Record<string, unknown>): SessionEvent => ({ seq, kind, payload, created_at: "2026-09-21T00:00:00Z" });

describe("latestContextUsage", () => {
  it("does not manufacture occupancy from categories or malformed values", () => {
    expect(latestContextUsage([event(1, "context", { usage: { categories: [{ name: "input", tokens: 12 }] } })])).toBeNull();
    expect(latestContextUsage([event(1, "context", { usage: { total_tokens: -1, context_window: 100 } })])).toBeNull();
    expect(latestContextUsage([event(1, "context", { usage: { total_tokens: 1, context_window: Number.NaN } })])).toBeNull();
  });

  it("projects substantiated provider telemetry and only valid category details", () => {
    expect(latestContextUsage([event(2, "context", { usage: { total_tokens: 25, context_window: 100, categories: [{ name: "system", tokens: 10 }, { name: "opaque" }] } })])).toEqual({
      currentTokens: 25, contextWindow: 100, percent: 25, label: "Provider-reported", categories: [{ name: "system", tokens: 10 }],
    });
  });

  it("labels explicit estimates and invalidates telemetry on a later model change", () => {
    expect(latestContextUsage([event(1, "context", { usage: { total_tokens: 50, context_window: 100, estimated: true } })])?.label).toBe("Estimated");
    expect(latestContextUsage([
      event(1, "context", { usage: { total_tokens: 50, context_window: 100 } }),
      event(2, "config", { model: "new-model" }),
    ])).toBeNull();
    expect(latestContextUsage([event(3, "context", { usage: { total_tokens: 50, context_window: 100 } })], 3)).toBeNull();
    expect(latestContextUsage([event(4, "context", { usage: { total_tokens: 50, context_window: 100 } })], 3)?.percent).toBe(50);
  });
});
