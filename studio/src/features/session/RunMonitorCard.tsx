import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useApprovalDetail, useJobLog, useProductRun, useProductRunArtifacts, useRunMetrics } from "@/api/hooks";
import type { Approval, ProductRunDetail } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

const ACTIVE_STATES = new Set(["queued", "preparing", "running", "stopping"]);
const STOPPABLE_STATES = new Set(["preparing", "running"]);
const STATE_LABELS: Record<string, string> = {
  queued: "Queued",
  preparing: "Preparing",
  running: "Running",
  stopping: "Stopping",
  succeeded: "Execution complete",
  failed: "Execution failed",
  cancelled: "Stopped",
  blocked: "Blocked",
  needs_attention: "Needs attention",
  awaiting_approval: "Awaiting confirmation",
  rejected: "Rejected",
};

function eventTimestamp(run: ProductRunDetail, kind: string): string | null {
  const item = run.timeline.items.find((candidate) => candidate.kind === kind);
  return typeof item?.timestamp === "string" ? item.timestamp : null;
}

function elapsedLabel(run: ProductRunDetail, now: number): string {
  const started = eventTimestamp(run, "job_started");
  if (!started) return "Unavailable";
  const terminal = eventTimestamp(run, "job_terminal");
  const startMs = Date.parse(started);
  const endMs = terminal ? Date.parse(terminal) : now;
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return "Unavailable";
  const seconds = Math.floor((endMs - startMs) / 1_000);
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const remainder = seconds % 60;
  return hours > 0 ? `${hours}h ${minutes}m ${remainder}s` : minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function collectionLabel(state: string | undefined): string {
  switch (state) {
    case "pending": return "Collecting results";
    case "delivered": return "Results ready";
    case "failed": return "Collection failed";
    default: return "Collection unavailable";
  }
}

export function RunMonitorCard({ planId, target }: { planId: string; target?: string | null }) {
  const run = useProductRun(planId);
  const active = ACTIVE_STATES.has(run.data?.state ?? "") || ["queued", "running"].includes(run.data?.canonical_job_status ?? "");
  const terminal = run.data?.terminal_result != null;
  const jobId = run.data?.job?.id ?? null;
  const metrics = useRunMetrics(jobId, active);
  const log = useJobLog(jobId, active);
  const artifacts = useProductRunArtifacts(planId, terminal);
  const [now, setNow] = useState(() => Date.now());
  const [stopApprovalId, setStopApprovalId] = useState<number | null>(null);
  const stopApproval = useApprovalDetail(stopApprovalId);
  const stop = useMutation({
    mutationFn: () => api<{ approval_id: number }>(`/api/v2/runs/${encodeURIComponent(planId)}/stop-requests`, {
      method: "POST",
      json: {},
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
    onSuccess: (result) => setStopApprovalId(result.approval_id),
  });

  const startedAt = run.data ? eventTimestamp(run.data, "job_started") : null;
  useEffect(() => {
    if (!active || !startedAt) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);

  if (run.isPending) return <Card className="space-y-2 border-sky-200"><CardTitle>Loading Run</CardTitle><p className="text-sm text-slate-500" role="status">Loading the canonical Run projection…</p></Card>;
  if (!run.data) return <Card className="space-y-2 border-amber-200"><CardTitle>Run transport unavailable</CardTitle><p className="text-sm text-amber-700">The Run projection cannot be reached. Execution state is unknown; retrying will not change execution truth.</p><Button onClick={() => void run.refetch()}>Retry</Button></Card>;

  const projection = run.data;
  const visibleMetrics = metrics.data?.metrics.slice(0, 4) ?? [];
  const stateLabel = projection.state === "needs_attention" && (
    projection.current_attempt?.liveness === "unknown" || projection.attention_reasons.includes("attempt_liveness_unknown")
  ) ? "Remote state unknown" : STATE_LABELS[projection.state] ?? projection.state;
  return (
    <Card className="space-y-3 border-sky-200" data-testid="run-monitor-card">
      <div className="flex flex-wrap items-center gap-2">
        <CardTitle className="mb-0">Run monitor</CardTitle>
        <Badge tone={projection.state === "succeeded" ? "ok" : projection.state === "failed" || projection.state === "cancelled" ? "bad" : active ? "info" : "warn"}>{stateLabel}</Badge>
        {run.isError ? <span className="text-xs text-amber-700" role="status">Transport unavailable · showing stale canonical state</span> : null}
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-slate-500">Target</dt><dd>{target || "Unavailable"}</dd>
        <dt className="text-slate-500">Elapsed</dt><dd>{elapsedLabel(projection, now)}</dd>
        <dt className="text-slate-500">Metrics</dt><dd>{metrics.isError ? "Unavailable (transport)" : metrics.data?.collection_status ?? projection.metrics_status ?? "unknown"}</dd>
        {visibleMetrics.map((metric) => <div key={metric.key} className="col-span-2 grid grid-cols-subgrid"><dt className="text-slate-500">{metric.key}</dt><dd>{metric.value_text}</dd></div>)}
        <dt className="text-slate-500">Results</dt><dd>{collectionLabel(projection.terminal_result?.collection_state)}</dd>
      </dl>
      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">Recent log · up to 80 lines</div>
        {log.isError ? <p className="text-xs text-amber-700">Log transport unavailable.</p> : <pre className="max-h-40 overflow-auto rounded bg-slate-950 p-2 text-xs text-slate-100">{log.data?.log_tail || "Log unavailable"}</pre>}
      </div>
      {STOPPABLE_STATES.has(projection.state) || (projection.state === "needs_attention" && projection.canonical_job_status === "running") ? <div className="space-y-2"><Button variant="danger" disabled={stop.isPending || stopApprovalId != null} onClick={() => stop.mutate()}>Request stop</Button>{stop.error ? <p className="text-xs text-rose-700">{(stop.error as Error).message}</p> : null}</div> : null}
      {stopApprovalId != null && stopApproval.isLoading ? <p className="text-sm text-slate-500">Loading stop confirmation…</p> : null}
      {stopApproval.data ? <ApprovalCard approval={stopApproval.data as Approval} decideVia="v2" approveLabel="Confirm stop" onDecided={() => { setStopApprovalId(null); void run.refetch(); }} /> : null}
      {stopApproval.isError ? <p className="text-xs text-rose-700">Stop confirmation is unavailable.</p> : null}
      {terminal ? <div className="text-sm"><div className="font-medium">Artifact metadata</div>{artifacts.isError ? <p className="text-xs text-amber-700">Artifact metadata unavailable.</p> : artifacts.data?.items.length ? <ul className="list-disc pl-5 text-xs text-slate-600">{artifacts.data.items.slice(0, 5).map((item) => <li key={`${item.relative_path}-${item.sha256}`}>{item.relative_path} · {item.size_bytes} bytes</li>)}</ul> : <p className="text-xs text-slate-500">{artifacts.data?.availability === "unknown" ? "Unavailable" : "No metadata reported"}</p>}</div> : null}
      <details className="text-xs text-slate-500"><summary className="cursor-pointer text-sky-700">Advanced Run details</summary><pre className="mt-2 max-h-64 overflow-auto rounded bg-slate-50 p-2">{JSON.stringify({ run: projection, metrics: metrics.data, log: log.data, artifacts: artifacts.data }, null, 2)}</pre></details>
    </Card>
  );
}
