import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useApprovalDetail, useJobLog, useProductRun, useProductRunArtifacts, useRunMetrics } from "@/api/hooks";
import type { Approval, ProductRunDetail } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { runAnalysisFor, type TranscriptItem } from "./transcript";

const ACTIVE_STATES = new Set(["queued", "preparing", "running", "stopping"]);
const STOPPABLE_STATES = new Set(["preparing", "running"]);
const STATE_LABELS: Record<string, string> = {
  queued: "排隊中",
  preparing: "準備中",
  running: "執行中",
  stopping: "停止中",
  succeeded: "執行完成",
  failed: "執行失敗",
  cancelled: "已停止",
  blocked: "已封鎖",
  needs_attention: "需要關注",
  awaiting_approval: "等待確認",
  rejected: "已拒絕",
};

function eventTimestamp(run: ProductRunDetail, kind: string): string | null {
  const item = run.timeline.items.find((candidate) => candidate.kind === kind);
  return typeof item?.timestamp === "string" ? item.timestamp : null;
}

function elapsedLabel(run: ProductRunDetail, now: number): string {
  const started = eventTimestamp(run, "job_started");
  if (!started) return "不可用";
  const terminal = eventTimestamp(run, "job_terminal");
  const startMs = Date.parse(started);
  const endMs = terminal ? Date.parse(terminal) : now;
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return "不可用";
  const seconds = Math.floor((endMs - startMs) / 1_000);
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const remainder = seconds % 60;
  return hours > 0 ? `${hours}h ${minutes}m ${remainder}s` : minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function collectionLabel(state: string | undefined): string {
  switch (state) {
    case "pending": return "正在收集結果";
    case "delivered": return "結果已就緒";
    case "failed": return "收集失敗";
    default: return "收集狀態不可用";
  }
}

export function RunMonitorCard({ planId, target, transcriptItems = [], analyzing = false, onAnalyzeRun = () => {} }: { planId: string; target?: string | null; transcriptItems?: TranscriptItem[]; analyzing?: boolean; onAnalyzeRun?: (planId: string) => void }) {
  const run = useProductRun(planId);
  const active = ACTIVE_STATES.has(run.data?.state ?? "") || ["queued", "running"].includes(run.data?.canonical_job_status ?? "");
  const terminal = run.data?.terminal_result != null;
  const analysis = runAnalysisFor(transcriptItems, planId);
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

  if (run.isPending) return <Card className="space-y-2 border-sky-200"><CardTitle>載入執行中…<span className="block text-xs font-normal text-slate-400">Loading Run</span></CardTitle><p className="text-sm text-slate-500" role="status">正在載入權威的執行狀態…</p></Card>;
  if (!run.data) return <Card className="space-y-2 border-amber-200"><CardTitle>執行通道不可用<span className="block text-xs font-normal text-slate-400">Run transport unavailable</span></CardTitle><p className="text-sm text-amber-700">無法連線至執行狀態。執行狀態未知；重試不會改變執行事實。</p><Button onClick={() => void run.refetch()}>重試</Button></Card>;

  const projection = run.data;
  const visibleMetrics = metrics.data?.metrics.slice(0, 4) ?? [];
  const stateLabel = projection.state === "needs_attention" && (
    projection.current_attempt?.liveness === "unknown" || projection.attention_reasons.includes("attempt_liveness_unknown")
  ) ? "遠端狀態未知" : STATE_LABELS[projection.state] ?? projection.state;
  return (
    <Card className="space-y-3 border-sky-200" data-testid="run-monitor-card">
      <div className="flex flex-wrap items-center gap-2">
        <CardTitle className="mb-0">執行監控<span className="block text-xs font-normal text-slate-400">Run monitor</span></CardTitle>
        <Badge tone={projection.state === "succeeded" ? "ok" : projection.state === "failed" || projection.state === "cancelled" ? "bad" : active ? "info" : "warn"}>{stateLabel}</Badge>
        {run.isError ? <span className="text-xs text-amber-700" role="status">傳輸不可用．顯示過期的權威狀態</span> : null}
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-slate-500">目標</dt><dd>{target || "不可用"}</dd>
        <dt className="text-slate-500">經過時間</dt><dd>{elapsedLabel(projection, now)}</dd>
        <dt className="text-slate-500">指標</dt><dd>{metrics.isError ? "不可用（傳輸中斷）" : metrics.data?.collection_status ?? projection.metrics_status ?? "未知"}</dd>
        {visibleMetrics.map((metric) => <div key={metric.key} className="col-span-2 grid grid-cols-subgrid"><dt className="text-slate-500">{metric.key}</dt><dd>{metric.value_text}</dd></div>)}
        <dt className="text-slate-500">結果</dt><dd>{collectionLabel(projection.terminal_result?.collection_state)}</dd>
      </dl>
      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">近期記錄．最多 80 行</div>
        {log.isError ? <p className="text-xs text-amber-700">記錄傳輸不可用。</p> : <pre className="max-h-40 overflow-auto rounded bg-slate-950 p-2 text-xs text-slate-100">{log.data?.log_tail || "記錄不可用"}</pre>}
      </div>
      {STOPPABLE_STATES.has(projection.state) || (projection.state === "needs_attention" && projection.canonical_job_status === "running") ? <div className="space-y-2"><Button variant="danger" disabled={stop.isPending || stopApprovalId != null} onClick={() => stop.mutate()}>要求停止</Button>{stop.error ? <p className="text-xs text-rose-700">{(stop.error as Error).message}</p> : null}</div> : null}
      {stopApprovalId != null && stopApproval.isLoading ? <p className="text-sm text-slate-500">載入停止確認中…</p> : null}
      {stopApproval.data ? <ApprovalCard approval={stopApproval.data as Approval} decideVia="v2" approveLabel="確認停止" onDecided={() => { setStopApprovalId(null); void run.refetch(); }} /> : null}
      {stopApproval.isError ? <p className="text-xs text-rose-700">停止確認不可用。</p> : null}
      {terminal ? <div className="text-sm"><div className="font-medium">產物中繼資料<span className="ml-1 text-xs font-normal text-slate-400">Artifact metadata</span></div>{artifacts.isError ? <p className="text-xs text-amber-700">產物中繼資料不可用。</p> : artifacts.data?.items.length ? <ul className="list-disc pl-5 text-xs text-slate-600">{artifacts.data.items.slice(0, 5).map((item) => <li key={`${item.relative_path}-${item.sha256}`}>{item.relative_path} · {item.size_bytes} bytes</li>)}</ul> : <p className="text-xs text-slate-500">{artifacts.data?.availability === "unknown" ? "不可用" : "無中繼資料回報"}</p>}</div> : null}
      {terminal ? <div className="space-y-1"><Button disabled={analysis.requested || analyzing} onClick={() => onAnalyzeRun(planId)}>{analysis.completed ? "分析完成" : analysis.requested ? "已要求分析" : "分析結果"}</Button><p className="text-xs text-slate-500">Agent 將檢視有限範圍的平台證據，並提出建議後停止。</p></div> : null}
      <details className="text-xs text-slate-500"><summary className="cursor-pointer text-sky-700">進階執行詳情<span className="ml-1 text-xs font-normal text-slate-400">Advanced Run details</span></summary><pre className="mt-2 max-h-64 overflow-auto rounded bg-slate-50 p-2">{JSON.stringify({ run: projection, metrics: metrics.data, log: log.data, artifacts: artifacts.data }, null, 2)}</pre></details>
    </Card>
  );
}
