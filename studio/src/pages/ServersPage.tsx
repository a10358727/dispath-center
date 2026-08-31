import { useState } from "react";
import { api } from "@/api/client";
import { useIdleSummary, useLiveServers, useRunners, useServerOccupancy } from "@/api/hooks";
import type { Approval } from "@/api/types";
import { Badge, stateTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";

function GpuBar({ percent }: { percent: number | null | undefined }) {
  if (percent == null) return null;
  return (
    <div className="h-1.5 w-24 overflow-hidden rounded bg-slate-200" title={`GPU ${percent}%`}>
      <div className={percent > 80 ? "h-full bg-rose-500" : percent > 30 ? "h-full bg-amber-500" : "h-full bg-emerald-500"} style={{ width: `${Math.min(100, percent)}%` }} />
    </div>
  );
}

function idleText(seconds: number | null | undefined): string {
  if (seconds == null) return "";
  if (seconds >= 3600) return `已閒置 ${Math.floor(seconds / 3600)} 小時`;
  if (seconds >= 60) return `已閒置 ${Math.floor(seconds / 60)} 分`;
  return "";
}

export function ServersPage() {
  const runners = useRunners();
  const live = useLiveServers();
  const idle = useIdleSummary();
  const occupancy = useServerOccupancy();
  const idleByName = new Map((idle.data ?? []).map((row) => [row.server_name, row]));
  const runnerByServer = new Map((runners.data?.runners ?? []).map((runner) => [runner.server, runner]));
  const [server, setServer] = useState("");
  const [label, setLabel] = useState("");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [error, setError] = useState<string | null>(null);
  const enroll = async () => {
    setError(null);
    try {
      const result = await api<{ approval: Approval }>("/api/v2/agent-runners/enroll-requests", { method: "POST", json: { server, label: label || null } });
      setApproval(result.approval);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };
  return (
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">伺服器與硬體</h1>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {(live.data ?? []).map((server) => {
          const runner = runnerByServer.get(server.name);
          const summary = idleByName.get(server.name);
          const counts = occupancy.data?.counts[server.name];
          return (
            <Card key={server.name} className="space-y-2">
              <CardTitle className="mb-0 flex items-center gap-2">
                <span className={server.online ? "text-emerald-500" : "text-slate-300"}>●</span>
                {server.name}
                {server.enabled === false ? <Badge tone="bad">已停用</Badge> : null}
              </CardTitle>
              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
                {(server.gpus ?? []).map((gpu, index) => (
                  <span key={index} className="flex items-center gap-1">
                    GPU{index} <GpuBar percent={gpu.util_percent} />
                    {gpu.mem_total_mb ? `${Math.round((gpu.mem_used_mb ?? 0) / 1024)}/${Math.round(gpu.mem_total_mb / 1024)}G` : ""}
                  </span>
                ))}
                {counts ? <span>執行 {counts.running}｜排隊 {counts.queued}</span> : null}
                {summary ? <span className="text-emerald-700">{idleText(summary.continuous_idle_seconds)}</span> : null}
                {summary?.gpu_util_p50 != null ? <span>24h GPU p50 {summary.gpu_util_p50}%</span> : null}
              </div>
              <div className="flex items-center gap-1">
                {runner ? (
                  <>
                    <Badge tone={runner.connected ? "ok" : "neutral"}>{runner.connected ? "agent 已連線" : "agent 離線"}</Badge>
                    <Badge tone={stateTone(runner.active ? "ok" : "failed")}>{runner.status}</Badge>
                    <span className="text-xs text-slate-400">runner {runner.id.slice(0, 8)}</span>
                  </>
                ) : (
                  <span className="text-xs text-slate-400">尚無 runner agent（SSH 派工照常）</span>
                )}
              </div>
            </Card>
          );
        })}
      </div>
      <Card className="space-y-2">
        <CardTitle>登錄 runner agent</CardTitle>
        {approval ? (
          <ApprovalCard approval={approval} onDecided={() => void runners.refetch()} />
        ) : (
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-sm">
              <span className="block text-slate-600">伺服器名稱（servers.yaml）</span>
              <input className="rounded border border-slate-300 px-2 py-1" value={server} onChange={(event) => setServer(event.target.value)} />
            </label>
            <label className="text-sm">
              <span className="block text-slate-600">標籤（選填）</span>
              <input className="rounded border border-slate-300 px-2 py-1" value={label} onChange={(event) => setLabel(event.target.value)} />
            </label>
            <Button variant="primary" disabled={!server} onClick={() => void enroll()}>
              建立核准卡
            </Button>
            {error ? <span className="text-xs text-rose-700">{error}</span> : null}
          </div>
        )}
        <p className="text-xs text-slate-500">核准後會顯示一次性的登錄憑證；存到 runner 的 <code>~/.dispatch-agent/credential</code>（0600）。runner 只出站連回這裡，工作機不開任何入站埠。</p>
      </Card>
    </div>
  );
}
