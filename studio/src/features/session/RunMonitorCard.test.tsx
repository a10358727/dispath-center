import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { approvedRunPlanId } from "./ReadyToRunCard";
import { RunMonitorCard } from "./RunMonitorCard";
import type { Approval, ProductRunDetail } from "@/api/types";

const PLAN_ID = "33333333-3333-4333-8333-333333333333";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function run(overrides: Partial<ProductRunDetail> = {}): ProductRunDetail {
  return {
    plan_id: PLAN_ID,
    project_id: "44444444-4444-4444-8444-444444444444",
    project_name: "demo",
    state: "running",
    metrics_status: "collected",
    canonical_job_status: "running",
    current_attempt: { id: "attempt-1", state: "running", liveness: "known" },
    attention_reasons: [],
    job: { id: 8, status: "running", created_at: "2026-09-21T00:00:00Z", started_at: "2026-09-21T00:00:05Z", finished_at: null, stalled_suspect: false },
    terminal_result: null,
    timeline: { items: [{ kind: "job_started", timestamp: "2026-09-21T00:00:05Z" }], truncated: false },
    ...overrides,
  };
}

function renderMonitor() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const result = render(<QueryClientProvider client={client}><MemoryRouter><RunMonitorCard planId={PLAN_ID} target="compute-a" /></MemoryRouter></QueryClientProvider>);
  return { ...result, client };
}

afterEach(() => vi.unstubAllGlobals());

describe("RunMonitorCard", () => {
  it("recovers the durable plan id only from verified approved detail", () => {
    const detail = { id: 41, created_at: "2026-09-21T00:00:00Z", kind: "execution_plan_v2", status: "approved", payload_verified: true, review: { execution_plan_id: PLAN_ID, contract: { contract_version: "execution-plan-v2" } } } as Approval;
    expect(approvedRunPlanId(detail)).toBe(PLAN_ID);
    expect(approvedRunPlanId({ ...detail, status: "pending" })).toBeNull();
    expect(approvedRunPlanId({ ...detail, payload_verified: false })).toBeNull();
    expect(approvedRunPlanId({ ...detail, review: { execution_plan_id: "not-a-plan" } })).toBeNull();
  });

  it("projects active state, authoritative elapsed time, bounded metrics, and the 80-line log route", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); calls.push(url);
      if (url === `/api/v2/runs/${PLAN_ID}`) return response(run());
      if (url === "/jobs/8/metrics") return response({ job_id: 8, collection_status: "collected", metrics: ["loss", "epoch", "accuracy", "rate", "hidden"].map((key, index) => ({ key, value_type: "decimal", value_text: String(index) })) });
      if (url === "/api/v2/jobs/8/log?lines=80") return response({ job_id: 8, status: "running", live: true, log_tail: "epoch 3" });
      return response({}, 404);
    }));
    renderMonitor();
    expect(await screen.findByText("Running")).toBeInTheDocument();
    expect(screen.getByText("compute-a")).toBeInTheDocument();
    expect(await screen.findByText("epoch 3")).toBeInTheDocument();
    expect(await screen.findByText("loss")).toBeInTheDocument();
    expect(screen.queryByText("hidden")).not.toBeInTheDocument();
    expect(calls).toContain("/api/v2/jobs/8/log?lines=80");
    expect(screen.getByText(/\d+[hms]/)).toBeInTheDocument();
  });

  it("keeps canonical state stale when polling transport disconnects", async () => {
    let fail = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v2/runs/${PLAN_ID}`) return fail ? Promise.reject(new Error("offline")) : response(run({ state: "needs_attention", current_attempt: { id: "attempt-1", state: "running", liveness: "unknown" } }));
      if (url === "/jobs/8/metrics" || url.includes("/log?lines=80")) return response({}, 503);
      return response({}, 404);
    }));
    const { client } = renderMonitor();
    expect(await screen.findByText("Remote state unknown")).toBeInTheDocument();
    expect(screen.queryByText("Execution failed")).not.toBeInTheDocument();
    expect(screen.getByText(/"state": "needs_attention"/)).toBeInTheDocument();
    fail = true;
    await client.invalidateQueries({ queryKey: ["product-run", PLAN_ID] });
    expect(await screen.findByText("Transport unavailable · showing stale canonical state")).toBeInTheDocument();
    expect(screen.getByText("Remote state unknown")).toBeInTheDocument();
  });

  it.each([
    ["pending", "Collecting results"],
    ["failed", "Collection failed"],
  ])("keeps %s result collection separate from successful execution", async (collectionState, label) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === `/api/v2/runs/${PLAN_ID}`) return response(run({ state: collectionState === "failed" ? "needs_attention" : "succeeded", canonical_job_status: "done", job: { ...run().job!, status: "done", finished_at: "2026-09-21T00:01:05Z" }, terminal_result: { canonical_job_status: "done", exit_code: 0, finished_at: "2026-09-21T00:01:05Z", collection_state: collectionState }, timeline: { items: [{ kind: "job_started", timestamp: "2026-09-21T00:00:05Z" }, { kind: "job_terminal", timestamp: "2026-09-21T00:01:05Z" }], truncated: false } }));
      if (url === "/jobs/8/metrics") return response({ job_id: 8, collection_status: "unknown", metrics: [] });
      if (url.includes("/log?lines=80")) return response({ job_id: 8, status: "done", live: false, log_tail: "complete" });
      if (url === `/api/v2/runs/${PLAN_ID}/artifacts?limit=5`) return response({ availability: "known", metadata_only: true, complete: false, items: [{ relative_path: "result.bin", kind: "file", size_bytes: 12, sha256: "a".repeat(64), reported_at: "2026-09-21T00:01:06Z", metadata_only: true }] });
      return response({}, 404);
    }));
    renderMonitor();
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.getByText("1m 0s")).toBeInTheDocument();
    expect(await screen.findByText("result.bin · 12 bytes")).toBeInTheDocument();
  });

  it("requires a human click to request analysis and does not call a Run mutation", async () => {
    const calls: { url: string; method: string }[] = [];
    const onAnalyzeRun = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); calls.push({ url, method: init?.method ?? "GET" });
      if (url === `/api/v2/runs/${PLAN_ID}`) return response(run({ state: "succeeded", canonical_job_status: "done", job: { ...run().job!, status: "done", finished_at: "2026-09-21T00:01:05Z" }, terminal_result: { canonical_job_status: "done", exit_code: 0, finished_at: "2026-09-21T00:01:05Z", collection_state: "delivered" } }));
      if (url === "/jobs/8/metrics") return response({ job_id: 8, collection_status: "collected", metrics: [] });
      if (url.includes("/log?lines=80")) return response({ job_id: 8, status: "done", live: false, log_tail: "complete" });
      if (url.includes("/artifacts?limit=5")) return response({ availability: "known", metadata_only: true, complete: true, items: [] });
      return response({}, 404);
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter><RunMonitorCard planId={PLAN_ID} transcriptItems={[]} onAnalyzeRun={onAnalyzeRun} /></MemoryRouter></QueryClientProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Analyze result" }));
    expect(onAnalyzeRun).toHaveBeenCalledWith(PLAN_ID);
    expect(calls.some(({ method }) => method !== "GET")).toBe(false);
  });

  it("requests governed stop and decides its approval without any job stop or cancel call", async () => {
    const calls: { url: string; method: string }[] = [];
    const stopApproval = { id: 52, kind: "stop", title: "Stop Run", status: "pending", created_at: "2026-09-21T00:00:00Z", payload_verified: true, payload_digest: "a".repeat(64), can_decide: true };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); calls.push({ url, method: init?.method ?? "GET" });
      if (url === `/api/v2/runs/${PLAN_ID}`) return response(run());
      if (url === "/jobs/8/metrics") return response({ job_id: 8, collection_status: "unknown", metrics: [] });
      if (url.includes("/log?lines=80")) return response({ job_id: 8, status: "running", live: false, log_tail: "active" });
      if (url === `/api/v2/runs/${PLAN_ID}/stop-requests`) return response({ approval_id: 52 }, 202);
      if (url === "/api/v2/approvals/52" && !init?.method) return response(stopApproval);
      if (url === "/api/v2/approvals/52/decisions") return response({ status: "approved" }, 202);
      return response({}, 404);
    }));
    renderMonitor();
    fireEvent.click(await screen.findByRole("button", { name: "Request stop" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm stop" }));
    await waitFor(() => expect(calls).toContainEqual({ url: "/api/v2/approvals/52/decisions", method: "POST" }));
    expect(calls).toContainEqual({ url: `/api/v2/runs/${PLAN_ID}/stop-requests`, method: "POST" });
    expect(calls.some(({ url }) => /\/api\/v2\/jobs\/8\/(stop-requests|cancel)/.test(url))).toBe(false);
  });
});
